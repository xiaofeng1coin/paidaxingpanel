import os
import sys
import time
import shutil
import logging
import threading
from datetime import datetime, timedelta
from sqlalchemy import text
from backend.models import db, Task, Dependency, Subscription, TaskView, SystemConfig, LoginLog
from backend.core.cron_utils import build_cron_trigger
from backend.extensions import scheduler, socketio
from backend.core.paths import LOGS_DIR, SCRIPTS_DIR, BASE_DIR, CONFIG_FILE
from backend.core.task_helpers import parse_script_meta, ensure_task_view
from backend.services.executors import execute_subscription, enqueue_task_execution, start_task_queue_worker

logger = logging.getLogger('scheduler')

def add_job_to_scheduler(app, task):
    if getattr(task, 'is_disabled', 0) == 1: return
    job_id = f"task_{task.id}"
    if scheduler.get_job(job_id): scheduler.remove_job(job_id)
    try:
        tz_str = os.environ.get('TZ', 'Asia/Shanghai')
        scheduler.add_job(enqueue_task_execution, build_cron_trigger(task.cron, timezone=tz_str), args=[app, task.id, False, 'schedule'], id=job_id)
    except Exception as e:
        # [修复 P2-1] cron 注册失败原先完全静默，用户无法知道任务为何从不执行
        logger.error("任务[%s]定时规则非法，未注册调度: cron=%r (%s)", task.name, task.cron, e)
        try:
            socketio.emit('log_stream', {'task_id': task.id, 'data': f"⚠️ 任务[{task.name}]的 cron 规则非法({task.cron})，定时调度未注册，请修正规则后重新启用\n"})
        except Exception:
            pass

def add_sub_job_to_scheduler(app, sub):
    if getattr(sub, 'is_disabled', 0) == 1: return
    job_id = f"sub_{sub.id}"
    if scheduler.get_job(job_id): scheduler.remove_job(job_id)
    try:
        tz_str = os.environ.get('TZ', 'Asia/Shanghai')
        if sub.cron:
            scheduler.add_job(execute_subscription, build_cron_trigger(sub.cron, timezone=tz_str), args=[app, sub.id], id=job_id)
    except Exception as e:
        logger.error("订阅[%s]定时规则非法，未注册调度: cron=%r (%s)", sub.name, sub.cron, e)
        try:
            socketio.emit('log_stream', {'task_id': f"sub_{sub.id}", 'data': f"⚠️ 订阅[{sub.name}]的 cron 规则非法({sub.cron})，定时调度未注册，请修正规则后重新启用\n"})
        except Exception:
            pass

def auto_clean_logs(app):
    with app.app_context():
        try:
            clean_cfg = SystemConfig.query.filter_by(key='log_clean_days').first()
            days = int(clean_cfg.value) if clean_cfg and str(clean_cfg.value).isdigit() else 7
            cutoff = datetime.now() - timedelta(days=days)
            for root, dirs, files in os.walk(LOGS_DIR):
                for file in files:
                    if file.endswith('.log'):
                        filepath = os.path.join(root, file)
                        if datetime.fromtimestamp(os.path.getmtime(filepath)) < cutoff:
                            try:
                                os.remove(filepath)
                            except OSError:
                                pass
            # [修复 P2-14] 清理已被清空的日志目录，避免空目录无限累积
            for root, dirs, files in os.walk(LOGS_DIR, topdown=False):
                if root != LOGS_DIR and not os.listdir(root):
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
            # [优化] 登录日志随同一保留期自动清理（login_time 为同格式字符串，可直接比较）
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
            LoginLog.query.filter(LoginLog.login_time < cutoff_str).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            logger.warning("日志自动清理任务异常", exc_info=True)

def ensure_auto_import_exclude_config():
    default_excludes = [
        'ql_env.js',
        'sys_notify.js',
        'sendNotify.js',
        'ql.js',
        'common.js',
        'utils.js',
        'util.js',
        'david_cookies.js',
        'xfj_sign.js',
        'untils.js'
    ]

    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write("# 派大星面板 Global Config\n# Format: export KEY=\"VALUE\"\n\n")
            f.write("# 全局任务最大执行时间(小时)，支持小数如0.1\n")
            f.write("export TASK_TIMEOUT=\"1\"\n\n")

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        content = ""

    if 'AUTO_IMPORT_EXCLUDE_FILES' not in content:
        append_text = "\n# 自动扫描 scripts 目录时排除的文件名，多个用英文逗号分隔\n"
        append_text += f"export AUTO_IMPORT_EXCLUDE_FILES=\"{','.join(default_excludes)}\"\n"
        with open(CONFIG_FILE, 'a', encoding='utf-8') as f:
            f.write(append_text)

def get_auto_import_exclude_files():
    ensure_auto_import_exclude_config()
    exclude_files = set()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip().replace('\ufeff', '')
                if not line.startswith('export AUTO_IMPORT_EXCLUDE_FILES='):
                    continue
                value = line.split('=', 1)[1].strip()
                if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                for item in value.split(','):
                    item = item.strip()
                    if item:
                        exclude_files.add(item)
                break
    except Exception:
        logger.warning("读取自动导入排除清单失败", exc_info=True)
    return exclude_files

def _read_config_flag(name):
    """读取 config.sh 中的布尔开关（export NAME="0"/"false"）；未配置返回 None（默认行为）。
    [加固 P2-9] 共享存储的 scripts 目录可被其他应用投放脚本并被自动收编执行，
    提供 AUTO_IMPORT_ENABLED 开关，部署在共享存储环境时建议显式关闭自动导入。"""
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for raw_line in f:
                line = raw_line.strip().replace('\ufeff', '')
                if line.startswith(f'export {name}='):
                    value = line.split('=', 1)[1].strip().strip('"').strip("'").lower()
                    if value in ('0', 'false', 'off', 'no'):
                        return False
                    if value in ('1', 'true', 'on', 'yes'):
                        return True
                    return None
    except Exception:
        pass
    return None


def auto_import_local_scripts(app):
    with app.app_context():
        try:
            # [加固 P2-9] AUTO_IMPORT_ENABLED="0" 时跳过自动导入（防止共享存储脚本被投毒自动执行）
            if _read_config_flag('AUTO_IMPORT_ENABLED') is False:
                return

            existing_commands = {t.command for t in Task.query.all()}
            default_cron = '0 0 * * *'
            exclude_files = get_auto_import_exclude_files()

            sub_aliases = {s.alias for s in Subscription.query.all() if s.alias}
            fixed_exclude_dirs = {'single_scripts'}

            for root, dirs, files in os.walk(SCRIPTS_DIR):
                rel_root = os.path.relpath(root, SCRIPTS_DIR).replace('\\', '/')
                if rel_root == '.':
                    rel_root = ''

                if '.git' in rel_root.split('/') or '__pycache__' in rel_root.split('/'):
                    continue

                path_parts = set(rel_root.split('/')) if rel_root else set()
                if path_parts & fixed_exclude_dirs:
                    continue
                if path_parts & sub_aliases:
                    continue

                dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__'] and d not in fixed_exclude_dirs and d not in sub_aliases]

                for file_name in files:
                    if not file_name.endswith(('.js', '.py', '.sh')):
                        continue
                    if file_name in exclude_files:
                        continue

                    full_path = os.path.join(root, file_name)
                    rel_path = os.path.relpath(full_path, SCRIPTS_DIR).replace('\\', '/')

                    if rel_path in existing_commands:
                        continue

                    # [修复 P2-10] 内存集合已覆盖全部存量 command，删除原先的逐文件 DB 查询
                    script_name, script_cron = parse_script_meta(full_path, os.path.splitext(os.path.basename(file_name))[0])
                    cron_to_use = script_cron if script_cron else default_cron

                    new_task = Task(
                        name=script_name,
                        command=rel_path,
                        cron=cron_to_use,
                        status='Idle',
                        source_type='manual',
                        source_key='manual',
                        source_name='单脚本'
                    )
                    db.session.add(new_task)
                    db.session.commit()

                    # [修复 P2-22] TaskView 创建统一走 ensure_task_view
                    ensure_task_view('单脚本', 'manual', 'manual', is_system=1, sort_order=0)

                    add_job_to_scheduler(app, new_task)
                    existing_commands.add(rel_path)
        except Exception:
            logger.warning("本地脚本自动导入异常", exc_info=True)

def _ensure_column(engine, table, column, ddl):
    """[修复 P2-25] 轻量列迁移：列不存在时执行 DDL 补齐，集中替代原先成片的 try-SELECT/ALTER 模板代码。
    返回 True 表示本次执行了迁移。"""
    try:
        with engine.connect() as conn:
            conn.execute(text(f'SELECT {column} FROM {table} LIMIT 1'))
        return False
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))
        print(f"Successfully migrated {table}.{column}")
        return True
    except Exception:
        logger.warning("迁移 %s.%s 失败", table, column, exc_info=True)
        return False


def run_scheduler_forever(app):
    with app.app_context():
        db.create_all()

        engine = db.engines['tasks']
        # [加固] 凭据版本号列（改密后使其他会话失效）
        _ensure_column(db.engine, 'user', 'cred_version', 'cred_version INTEGER DEFAULT 0')
        _ensure_column(engine, 'task', 'last_duration', 'last_duration VARCHAR(20) DEFAULT "-"')
        _ensure_column(engine, 'task', 'is_disabled', 'is_disabled INTEGER DEFAULT 0')
        _ensure_column(engine, 'subscription', 'alias', 'alias VARCHAR(50) DEFAULT "sub_default"')
        _ensure_column(engine, 'subscription', 'is_disabled', 'is_disabled INTEGER DEFAULT 0')
        _ensure_column(engine, 'task', 'source_type', "source_type VARCHAR(20) DEFAULT 'manual'")
        _ensure_column(engine, 'task', 'source_key', "source_key VARCHAR(100) DEFAULT 'manual'")
        _ensure_column(engine, 'task', 'source_name', "source_name VARCHAR(100) DEFAULT '单脚本'")

        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE task SET status = 'Idle' WHERE status IN ('Running', 'Queued')"))
        except Exception:
            logger.warning("复位遗留任务状态失败", exc_info=True)

        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE task SET source_type = 'manual' WHERE source_type IS NULL OR source_type = ''"))
                conn.execute(text("UPDATE task SET source_key = 'manual' WHERE source_key IS NULL OR source_key = ''"))
                conn.execute(text("UPDATE task SET source_name = '单脚本' WHERE source_name IS NULL OR source_name = ''"))
        except Exception:
            logger.warning("补齐任务来源字段失败", exc_info=True)

        env_engine = db.engines['envs']
        _ensure_column(env_engine, 'env', 'is_disabled', 'is_disabled INTEGER DEFAULT 0')
        _ensure_column(env_engine, 'env', 'updated_at', 'updated_at VARCHAR(50) DEFAULT "-"')
        _ensure_column(env_engine, 'env', 'position', 'position INTEGER DEFAULT 0')

        try:
            with env_engine.begin() as conn:
                conn.execute(text('UPDATE env SET position = id WHERE position = 0 OR position IS NULL'))
        except Exception:
            pass

        try:
            Task.query.filter(Task.status.in_(['Running', 'Queued'])).update({'status': 'Idle'})
            Dependency.query.filter(Dependency.status.in_(['Installing', 'Uninstalling'])).update({'status': 'Error'})
            Subscription.query.filter_by(status='Running').update({'status': 'Idle'})
            db.session.commit()
        except Exception:
            logger.warning("复位遗留状态失败", exc_info=True)

        try:
            ensure_task_view('单脚本', 'manual', 'manual', is_system=1, sort_order=0)
        except Exception:
            logger.warning("初始化单脚本视图失败", exc_info=True)

        try:
            sub_tasks = db.session.query(Task.source_key, Task.source_name, Task.source_type).filter(
                Task.source_key.isnot(None),
                Task.source_key != '',
                Task.source_type == 'subscription'
            ).distinct().all()

            max_order = db.session.query(db.func.max(TaskView.sort_order)).scalar() or 0

            for source_key, source_name, source_type in sub_tasks:
                exists = TaskView.query.filter_by(source_key=source_key).first()
                if not exists:
                    max_order += 1
                    db.session.add(TaskView(
                        name=source_name or source_key,
                        source_key=source_key,
                        source_type=source_type or 'subscription',
                        is_visible=0,
                        is_system=0,
                        sort_order=max_order
                    ))
                else:
                    changed = False
                    if exists.name != (source_name or source_key):
                        exists.name = source_name or source_key
                        changed = True
                    if exists.source_type != (source_type or 'subscription'):
                        exists.source_type = source_type or 'subscription'
                        changed = True
            db.session.commit()
        except Exception:
            logger.warning("从任务来源重建订阅视图失败", exc_info=True)

        ql_env_path = os.path.join(SCRIPTS_DIR, 'ql_env.js')
        with open(ql_env_path, 'w', encoding='utf-8') as f:
            f.write("""if (!console.logErr) { console.logErr = function(e) { console.error(e.message || e); }; }

// 彻底拦截 dotenvx 的输出(拦截底层标准输出/错误流)
const originalStdoutWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = function(chunk, encoding, callback) {
    if (typeof chunk === 'string' && chunk.includes('[dotenv@')) return true;
    return originalStdoutWrite(chunk, encoding, callback);
};
const originalStderrWrite = process.stderr.write.bind(process.stderr);
process.stderr.write = function(chunk, encoding, callback) {
    if (typeof chunk === 'string' && chunk.includes('[dotenv@')) return true;
    return originalStderrWrite(chunk, encoding, callback);
};

const _log = console.log;
console.log = function(...args) {
    if (typeof args[0] === 'string' && args[0].includes('[dotenv@')) return;
    _log.apply(console, args);
};

// 【核心机制拦截】防止第三方混淆JS脚本检测 process.env 包含 GITHUB 字样而触发防盗链并直接 process.exit(0)
const originalStringify = JSON.stringify;
JSON.stringify = function(value, replacer, space) {
    let result = originalStringify(value, replacer, space);
    if (value === process.env && typeof result === 'string') {
        result = result.replace(/GITHUB/g, 'G_I_T_H_U_B');
    }
    return result;
};
""")

        sys_notify_path = os.path.join(SCRIPTS_DIR, 'sys_notify.js')
        if not os.path.exists(sys_notify_path):
            with open(sys_notify_path, 'w', encoding='utf-8') as f:
                f.write("""try {
    const { sendNotify } = require('./sendNotify.js');
    const title = process.argv[2];
    const content = process.argv[3];
    sendNotify(title, content, {}, '').then(() => process.exit(0)).catch(() => process.exit(1));
} catch(e) {
    console.error('无法调用 sendNotify.js:', e.message);
    process.exit(1);
}""")

        for folder_name in ['init_scripts', 'init-scripts']:
            init_scripts_dir = os.path.join(BASE_DIR, folder_name)
            if os.path.exists(init_scripts_dir):
                for file_name in os.listdir(init_scripts_dir):
                    if file_name.endswith('.js'):
                        src_file = os.path.join(init_scripts_dir, file_name)
                        dst_file = os.path.join(SCRIPTS_DIR, file_name)
                        if not os.path.exists(dst_file):
                            try:
                                shutil.copy2(src_file, dst_file)
                                print(f"[System] ✅ 初始化脚本已自动复制: {file_name}", flush=True)
                            except Exception as e:
                                pass

        ensure_auto_import_exclude_config()

        if not scheduler.running:
            try:
                tc_cfg = SystemConfig.query.filter_by(key='task_concurrency').first()
                if tc_cfg and str(tc_cfg.value).isdigit():
                    from apscheduler.executors.pool import ThreadPoolExecutor
                    scheduler.configure(executors={'default': ThreadPoolExecutor(int(tc_cfg.value))})
            except Exception:
                logger.warning("调度器线程池配置失败，使用默认配置", exc_info=True)

            try:
                tz_cfg = SystemConfig.query.filter_by(key='timezone').first()
                if tz_cfg and tz_cfg.value:
                    os.environ['TZ'] = tz_cfg.value
                    try:
                        time.tzset()
                    except AttributeError:
                        pass
            except Exception:
                logger.warning("时区配置应用失败", exc_info=True)

            scheduler.start()
            start_task_queue_worker(app)

            if not scheduler.get_job('sys_log_clean'):
                tz_str = os.environ.get('TZ', 'Asia/Shanghai')
                scheduler.add_job(auto_clean_logs, build_cron_trigger('0 2 * * *', timezone=tz_str), args=[app], id='sys_log_clean')

            # [修复 P2-10] 自动导入由 10 秒降为 60 秒，降低大目录下的无效扫描开销
            if not scheduler.get_job('sys_auto_import_local_scripts'):
                scheduler.add_job(auto_import_local_scripts, 'interval', seconds=60, args=[app], id='sys_auto_import_local_scripts', max_instances=1, coalesce=True)

            try:
                auto_import_local_scripts(app)
                for task in Task.query.all():
                    add_job_to_scheduler(app, task)
                for sub in Subscription.query.all():
                    add_sub_job_to_scheduler(app, sub)
            except Exception:
                logger.warning("启动任务/订阅注册失败", exc_info=True)

            print("\n================ 调度器启动成功 ================")
            print(f"当前环境变量 TZ: {os.environ.get('TZ', '未设置')}")
            print("当前已加载的任务及其下一次执行时间:")
            for job in scheduler.get_jobs():
                print(f" - 任务ID: {job.id}, 下次运行时间: {getattr(job, 'next_run_time', '无法计算')}")
            print("================================================\n", flush=True)

            while True:
                time.sleep(60)

def start_scheduler_once(app):
    if not os.environ.get('SCHEDULER_STARTED'):
        os.environ['SCHEDULER_STARTED'] = '1'
        threading.Thread(target=run_scheduler_forever, args=(app,), daemon=True).start()