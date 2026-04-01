import os
import sys
import time
import shutil
import threading
from sqlalchemy import text
from apscheduler.triggers.cron import CronTrigger
from backend.models import db, Task, Dependency, Subscription, TaskView, SystemConfig
from backend.extensions import scheduler
from backend.core.paths import LOGS_DIR, SCRIPTS_DIR, BASE_DIR, CONFIG_FILE
from backend.core.task_helpers import parse_script_meta
from backend.services.executors import execute_subscription, enqueue_task_execution, start_task_queue_worker

def add_job_to_scheduler(app, task):
    if getattr(task, 'is_disabled', 0) == 1: return
    job_id = f"task_{task.id}"
    if scheduler.get_job(job_id): scheduler.remove_job(job_id)
    try:
        tz_str = os.environ.get('TZ', 'Asia/Shanghai')
        scheduler.add_job(enqueue_task_execution, CronTrigger.from_crontab(task.cron, timezone=tz_str), args=[app, task.id, False, 'schedule'], id=job_id)
    except:
        pass

def add_sub_job_to_scheduler(app, sub):
    if getattr(sub, 'is_disabled', 0) == 1: return
    job_id = f"sub_{sub.id}"
    if scheduler.get_job(job_id): scheduler.remove_job(job_id)
    try:
        tz_str = os.environ.get('TZ', 'Asia/Shanghai')
        if sub.cron:
            scheduler.add_job(execute_subscription, CronTrigger.from_crontab(sub.cron, timezone=tz_str), args=[app, sub.id], id=job_id)
    except:
        pass

def auto_clean_logs(app):
    with app.app_context():
        try:
            clean_cfg = SystemConfig.query.filter_by(key='log_clean_days').first()
            days = int(clean_cfg.value) if clean_cfg and str(clean_cfg.value).isdigit() else 7
            cutoff = __import__('datetime').datetime.now() - __import__('datetime').timedelta(days=days)
            for root, dirs, files in os.walk(LOGS_DIR):
                for file in files:
                    if file.endswith('.log'):
                        filepath = os.path.join(root, file)
                        if __import__('datetime').datetime.fromtimestamp(os.path.getmtime(filepath)) < cutoff:
                            os.remove(filepath)
        except:
            pass

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
    except:
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
    except:
        pass
    return exclude_files

def auto_import_local_scripts(app):
    with app.app_context():
        try:
            existing_commands = {t.command for t in Task.query.all()}
            default_cron = '0 0 * * *'
            exclude_files = get_auto_import_exclude_files()

            sub_aliases = {s.alias for s in Subscription.query.all() if s.alias}
            fixed_exclude_dirs = {'single_scripts'}

            for root, dirs, files in os.walk(SCRIPTS_DIR):
                rel_root = os.path.relpath(root, SCRIPTS_DIR).replace('\\', '/')
                if rel_root == '.':
                    rel_root = ''

                if '.git' in root or '__pycache__' in root:
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

                    task_exists = Task.query.filter_by(command=rel_path).first()
                    if task_exists:
                        existing_commands.add(rel_path)
                        continue

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

                    if not TaskView.query.filter_by(source_key='manual').first():
                        db.session.add(TaskView(
                            name='单脚本',
                            source_key='manual',
                            source_type='manual',
                            is_visible=0,
                            is_system=1,
                            sort_order=0
                        ))
                        db.session.commit()

                    add_job_to_scheduler(app, new_task)
                    existing_commands.add(rel_path)
        except:
            pass

def run_scheduler_forever(app):
    with app.app_context():
        db.create_all()

        engine = db.engines['tasks']
        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT last_duration FROM task LIMIT 1'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text('ALTER TABLE task ADD COLUMN last_duration VARCHAR(20) DEFAULT "-"'))
                    conn.execute(text('ALTER TABLE task ADD COLUMN is_disabled INTEGER DEFAULT 0'))
                print("Successfully migrated tasks.db with new columns.")
            except Exception:
                pass

        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT alias FROM subscription LIMIT 1'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text('ALTER TABLE subscription ADD COLUMN alias VARCHAR(50) DEFAULT "sub_default"'))
                print("Successfully migrated tasks.db subscription with alias column.")
            except Exception:
                pass

        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT is_disabled FROM subscription LIMIT 1'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text('ALTER TABLE subscription ADD COLUMN is_disabled INTEGER DEFAULT 0'))
                print("Successfully migrated tasks.db subscription with is_disabled column.")
            except Exception:
                pass

        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT source_type FROM task LIMIT 1'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE task ADD COLUMN source_type VARCHAR(20) DEFAULT 'manual'"))
                print("Successfully migrated tasks.db task with source_type column.")
            except Exception:
                pass

        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT source_key FROM task LIMIT 1'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE task ADD COLUMN source_key VARCHAR(100) DEFAULT 'manual'"))
                print("Successfully migrated tasks.db task with source_key column.")
            except Exception:
                pass

        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT source_name FROM task LIMIT 1'))
        except Exception:
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE task ADD COLUMN source_name VARCHAR(100) DEFAULT '单脚本'"))
                print("Successfully migrated tasks.db task with source_name column.")
            except Exception:
                pass

        try:
            with engine.connect() as conn:
                conn.execute(text('SELECT status FROM task LIMIT 1'))
            with engine.begin() as conn:
                conn.execute(text("UPDATE task SET status = 'Idle' WHERE status IN ('Running', 'Queued')"))
        except Exception:
            pass

        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE task SET source_type = 'manual' WHERE source_type IS NULL OR source_type = ''"))
                conn.execute(text("UPDATE task SET source_key = 'manual' WHERE source_key IS NULL OR source_key = ''"))
                conn.execute(text("UPDATE task SET source_name = '单脚本' WHERE source_name IS NULL OR source_name = ''"))
        except Exception:
            pass

        env_engine = db.engines['envs']
        try:
            with env_engine.connect() as conn:
                conn.execute(text('SELECT is_disabled FROM env LIMIT 1'))
        except Exception:
            try:
                with env_engine.begin() as conn:
                    conn.execute(text('ALTER TABLE env ADD COLUMN is_disabled INTEGER DEFAULT 0'))
                    conn.execute(text('ALTER TABLE env ADD COLUMN updated_at VARCHAR(50) DEFAULT "-"'))
                print("Successfully migrated envs.db with new columns.")
            except Exception:
                pass

        try:
            with env_engine.connect() as conn:
                conn.execute(text('SELECT position FROM env LIMIT 1'))
        except Exception:
            try:
                with env_engine.begin() as conn:
                    conn.execute(text('ALTER TABLE env ADD COLUMN position INTEGER DEFAULT 0'))
                    conn.execute(text('UPDATE env SET position = id'))
                print("Successfully migrated envs.db with position column.")
            except Exception:
                pass

        try:
            Task.query.filter(Task.status.in_(['Running', 'Queued'])).update({'status': 'Idle'})
            Dependency.query.filter(Dependency.status.in_(['Installing', 'Uninstalling'])).update({'status': 'Error'})
            Subscription.query.filter_by(status='Running').update({'status': 'Idle'})
            db.session.commit()
        except:
            pass

        try:
            if not TaskView.query.filter_by(source_key='manual').first():
                db.session.add(TaskView(
                    name='单脚本',
                    source_key='manual',
                    source_type='manual',
                    is_visible=0,
                    is_system=1,
                    sort_order=0
                ))
                db.session.commit()
        except:
            pass

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
        except:
            pass

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
            except:
                pass

            try:
                tz_cfg = SystemConfig.query.filter_by(key='timezone').first()
                if tz_cfg and tz_cfg.value:
                    os.environ['TZ'] = tz_cfg.value
                    try:
                        time.tzset()
                    except AttributeError:
                        pass
            except:
                pass

            scheduler.start()
            start_task_queue_worker(app)

            if not scheduler.get_job('sys_log_clean'):
                tz_str = os.environ.get('TZ', 'Asia/Shanghai')
                scheduler.add_job(auto_clean_logs, CronTrigger.from_crontab('0 2 * * *', timezone=tz_str), args=[app], id='sys_log_clean')

            if not scheduler.get_job('sys_auto_import_local_scripts'):
                scheduler.add_job(auto_import_local_scripts, 'interval', seconds=10, args=[app], id='sys_auto_import_local_scripts', max_instances=1, coalesce=True)

            try:
                auto_import_local_scripts(app)
                for task in Task.query.all():
                    add_job_to_scheduler(app, task)
                for sub in Subscription.query.all():
                    add_sub_job_to_scheduler(app, sub)
            except:
                pass

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