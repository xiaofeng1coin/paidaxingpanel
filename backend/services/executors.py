import os
import time
import re
import ast
import logging
import traceback
import subprocess
import threading
import urllib.request
import shutil
from datetime import datetime
from backend.models import db, Task, Env, Dependency, SystemConfig, Subscription, TaskView
from backend.extensions import socketio, scheduler
from backend.runtime import (
    running_processes, debug_processes, SUBPROCESS_KWARGS, queued_tasks, task_queue,
    last_auto_fixes, task_start_times
)
from backend.core.paths import SCRIPTS_DIR, LOGS_DIR, NODE_DIR, PYTHON_DIR, LINUX_DIR, CUSTOM_OVERRIDE_DIR
from backend.core.security import get_safe_path
from backend.core.env_manager import get_combined_env
from backend.core.dependency_manager import (
    run_dependency_install_sync, normalize_python_package_name,
    get_dependency_log_path, uninstall_pip_target_package
)
from backend.core.proc_utils import kill_process_tree
from backend.core.task_helpers import (
    parse_script_meta, ensure_task_view, sanitize_task_name, sanitize_dep_log_name,
    validate_alias, validate_package_name, sanitize_mirror_url
)
from backend.core.config_cache import get_all_configs

logger = logging.getLogger('executors')

_queue_lock = threading.Lock()
_queue_worker_started = False
_timeout_watcher_lock = threading.Lock()
_timeout_watcher_started = False


def emit_queue_status():
    try:
        socketio.emit('queue_status', {
            'queued_count': len(task_queue)
        })
    except Exception:
        logger.warning("队列状态推送失败", exc_info=True)


def get_task_concurrency_limit(app=None):
    # [优化] 队列 worker 每秒轮询并发上限，改走 30s TTL 缓存，不再每秒直查数据库
    try:
        if app is not None:
            raw = (get_all_configs(app) or {}).get('task_concurrency')
        else:
            cfg = SystemConfig.query.filter_by(key='task_concurrency').first()
            raw = cfg.value if cfg else None
        if raw and str(raw).isdigit():
            value = int(raw)
            return value if value > 0 else 5
    except Exception:
        logger.warning("并发上限配置读取失败，使用默认值 5", exc_info=True)
    return 5


def start_task_queue_worker(app):
    global _queue_worker_started
    with _queue_lock:
        if _queue_worker_started:
            return
        _queue_worker_started = True
    threading.Thread(target=task_queue_worker, args=(app,), daemon=True).start()


def enqueue_task_execution(app, task_id, ignore_disabled=False, trigger_source='schedule'):
    start_task_queue_worker(app)
    with app.app_context():
        task = db.session.get(Task, task_id)
        if not task:
            return False, "任务不存在"
        if getattr(task, 'is_disabled', 0) == 1 and not ignore_disabled:
            return False, "任务已禁用"
        if task_id in running_processes:
            return False, "任务运行中"
        if task_id in queued_tasks:
            return False, "任务已在队列中"

        queued_tasks[task_id] = {
            'task_id': task_id,
            'ignore_disabled': ignore_disabled,
            'trigger_source': trigger_source,
            'queued_at': time.time()
        }
        task_queue.append(task_id)
        task.status = 'Queued'
        if not task.last_run or task.last_run == 'Never':
            task.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.session.commit()
        socketio.emit('task_status', {'task_id': task.id, 'status': 'Queued', 'last_run': task.last_run})
        socketio.emit('log_stream', {'task_id': task.id, 'data': "⏳ 任务已进入执行队列，等待空闲运行槽位...\n"})
        emit_queue_status()
        return True, "已加入队列"


def task_queue_worker(app):
    while True:
        try:
            with app.app_context():
                limit = get_task_concurrency_limit(app)

                while len(running_processes) < limit and task_queue:
                    task_id = task_queue.pop(0)
                    queued_info = queued_tasks.pop(task_id, None)
                    emit_queue_status()
                    if not queued_info:
                        continue

                    task = db.session.get(Task, task_id)
                    if not task:
                        continue
                    if getattr(task, 'is_disabled', 0) == 1 and not queued_info.get('ignore_disabled', False):
                        task.status = 'Idle'
                        db.session.commit()
                        socketio.emit('task_status', {'task_id': task.id, 'status': 'Idle', 'duration': '-'})
                        continue

                    threading.Thread(
                        target=execute_task,
                        args=(app, task_id, queued_info.get('ignore_disabled', False), 0),
                        daemon=True
                    ).start()
        except Exception:
            # [修复 P2-1] 队列轮询异常不再静默吞掉，至少留下日志线索
            logger.warning("任务队列轮询异常", exc_info=True)

        time.sleep(1)


def get_subscription_override_dir(sub):
    if not sub or not sub.alias:
        return None
    override_dir = get_safe_path(CUSTOM_OVERRIDE_DIR, sub.alias)
    if not override_dir:
        return None
    return override_dir


def list_override_files(base_dir):
    rel_files = []
    if not base_dir or not os.path.exists(base_dir):
        return rel_files
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__']]
        for file_name in files:
            src_file = os.path.join(root, file_name)
            rel_path = os.path.relpath(src_file, base_dir).replace('\\', '/')
            rel_files.append(rel_path)
    rel_files.sort()
    return rel_files


def apply_custom_overrides_for_subscription(sub, target_dir, write_log=None):
    override_dir = get_subscription_override_dir(sub)
    if not override_dir or not os.path.exists(override_dir):
        if write_log:
            write_log("ℹ️ 未检测到该订阅的自定义覆盖目录，跳过覆盖保护步骤\n")
        return []

    override_files = list_override_files(override_dir)
    if not override_files:
        if write_log:
            write_log("ℹ️ 自定义覆盖目录存在，但未发现可覆盖文件，跳过覆盖保护步骤\n")
        return []

    applied_files = []
    if write_log:
        write_log(f"🛡️ 检测到自定义覆盖目录：{override_dir}\n")
        write_log(f"🛡️ 共发现 {len(override_files)} 个自定义文件，开始回填覆盖...\n")

    for rel_path in override_files:
        src_file = get_safe_path(override_dir, rel_path)
        dst_file = get_safe_path(target_dir, rel_path)
        if not src_file or not dst_file:
            if write_log:
                write_log(f"⚠️ 跳过非法覆盖路径：{rel_path}\n")
            continue
        if not os.path.exists(src_file):
            continue
        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
        shutil.copy2(src_file, dst_file)
        applied_files.append(rel_path)
        if write_log:
            write_log(f"✅ 已回填自定义文件：{rel_path}\n")

    if write_log:
        write_log(f"🛡️ 自定义覆盖回填完成，共处理 {len(applied_files)} 个文件\n\n")
    return applied_files


def diagnose_task_issue(app, output_text, returncode, duration_seconds):
    text = output_text or ""
    text_lower = text.lower()

    npm_match = re.search(r"cannot find module ['\"]([^'\"]+)['\"]", text, re.IGNORECASE)
    if npm_match:
        module_name = npm_match.group(1).strip()
        if not module_name.startswith('./') and not module_name.startswith('../') and not module_name.startswith('/'):
            return {
                'type': 'node_missing_dep',
                'title': f"检测到 Node.js 依赖缺失: {module_name}",
                'package': module_name,
                'pkg_type': 'npm',
                'auto_fix': True,
                'retriable': True,
                'reason': "缺少 nodejs 依赖",
                'solution': "系统将自动尝试安装该依赖并重跑脚本"
            }
        return {
            'type': 'local_missing_file',
            'title': f"检测到本地文件缺失: {module_name}",
            'auto_fix': False,
            'reason': "脚本同目录或者引用目录下缺少文件",
            'solution': "方法1、重新拉库\n方法2、自己手动进入对应路径下补全缺少的文件"
        }

    py_match = re.search(r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]", text, re.IGNORECASE)
    if py_match:
        module_name = py_match.group(1).strip()
        real_pkg_name = normalize_python_package_name(module_name)
        return {
            'type': 'python_missing_dep',
            'title': f"检测到 Python 依赖缺失: {module_name}",
            'package': real_pkg_name,
            'display_package': module_name,
            'pkg_type': 'pip',
            'auto_fix': True,
            'retriable': True,
            'reason': "缺少 python 依赖",
            'solution': f"系统将自动尝试安装依赖 {real_pkg_name} 并重跑脚本；若仍失败，请查询该模块在 pip 中的真实包名"
        }

    if 'this.got.post is not a function' in text_lower or 'got.extend is not a function' in text_lower:
        return {
            'type': 'got_version_issue',
            'title': "检测到 got 版本兼容问题",
            'package': 'got@11',
            'pkg_type': 'npm',
            'auto_fix': True,
            'retriable': True,
            'reason': "新版 got 与旧脚本调用方式不兼容",
            'solution': "系统将自动安装 got@11 并重跑脚本"
        }

    if returncode == 0 and duration_seconds < 1.5 and len(text.strip()) < 300:
        github_related = False
        with app.app_context():
            try:
                for e in Env.query.all():
                    if e.name and 'github' in e.name.lower():
                        github_related = True
                        break
            except Exception:
                logger.warning("环境变量扫描失败", exc_info=True)
        if github_related:
            return {
                'type': 'github_env_issue',
                'title': "检测到疑似 github 变量导致脚本秒退",
                'auto_fix': False,
                'reason': "变量中存在名字中带有 github 的变量名",
                'solution': "去环境变量或者配置文件中删除该变量即可，是整行删除，不是只删除变量值，目前常见于哔哩哔哩脚本中的 github 加速变量"
            }

    ck_zero_match = re.search(r'(?<![a-z0-9_])(jd_cookie|ck|cookie|cookies)\s*[为=:：]?\s*0\b', text_lower)
    has_valid_ck_signal = any(keyword in text_lower for keyword in [
        '登录成功',
        '开始任务',
        '用户：',
        '用户:',
        '获取sessionid',
        '获取signature_key',
        '获取code',
        '阅读抽奖',
        '签到id',
        '抽奖获得'
    ])
    if ck_zero_match and not has_valid_ck_signal and returncode != 0:
        return {
            'type': 'env_name_wrong',
            'title': "检测到脚本读取到 ck 为 0",
            'auto_fix': False,
            'reason': "变量名填写错了",
            'solution': "填写正确的变量名"
        }

    if 'sendnotify' in text_lower or '通知' in text or 'pushplus' in text_lower or 'telegram' in text_lower:
        notify_success_keywords = [
            '发送通知消息成功',
            '发送通知成功',
            'telegram发送通知消息成功',
            '钉钉发送通知消息成功',
            'push+发送',
            'wxpusher 发送通知消息成功',
            'server酱发送通知消息成功'
        ]
        if not any(k.lower() in text_lower for k in notify_success_keywords):
            if (
                '未配置' in text or
                'not configured' in text_lower or
                '未找到推送配置' in text or
                'push_key' in text_lower and '未填写' in text or
                'tg_bot_token' in text_lower and '未填写' in text or
                'dd_bot_token' in text_lower and '未填写' in text
            ):
                return {
                    'type': 'notify_config_issue',
                    'title': "检测到通知配置可能缺失",
                    'auto_fix': False,
                    'reason': "脚本通知参数未配置或脚本内通知开关未开启",
                    'solution': "在配置文件中配置通知参数\n看脚本注释是否有通知开关，有的话根据注释填写变量将其打开\n更换其他脚本测试通知"
                }

    return None


def write_diagnosis_message(write_log, diagnosis):
    if not diagnosis:
        return
    msg = "\n" \
          "================ 智能诊断报告 ================\n" \
          f"🔍 诊断结果: {diagnosis.get('title', '未知问题')}\n" \
          f"📌 可能原因: {diagnosis.get('reason', '-')}\n" \
          f"🛠️ 建议处理:\n{diagnosis.get('solution', '-')}\n" \
          "==============================================\n"
    write_log(msg)


def start_timeout_watcher():
    """[修复 P2-13] 启动共享超时扫描线程（全局仅一个，替代每任务一线程 sleep 的模式）。"""
    global _timeout_watcher_started
    with _timeout_watcher_lock:
        if _timeout_watcher_started:
            return
        _timeout_watcher_started = True
    threading.Thread(target=_timeout_watcher_loop, daemon=True).start()


def _timeout_watcher_loop():
    """共享超时扫描：每 5 秒检查已注册任务的执行时长，超时则强制终止进程树。"""
    while True:
        try:
            now = time.time()
            expired = [tid for tid, info in task_start_times.items() if now >= info.get('deadline', now + 1)]
            for task_id in expired:
                info = task_start_times.pop(task_id, None)
                if not info:
                    continue
                process = info.get('process')
                if process is not None and process.poll() is None:
                    # [修复 P1-4] 用进程树终止替换单纯 kill，避免脚本子进程残留继续运行
                    kill_process_tree(process)
                    warn_msg = f"\n❌ 任务执行超过最大设定时长 ({info.get('max_hours')} 小时)，已被系统强制终止！\n"
                    socketio.emit('log_stream', {'task_id': task_id, 'data': warn_msg})
                    log_path = info.get('log_path')
                    if log_path and os.path.exists(log_path):
                        try:
                            with open(log_path, 'a', encoding='utf-8') as lf:
                                lf.write(warn_msg)
                        except Exception:
                            pass
        except Exception:
            logger.warning("任务超时扫描线程异常", exc_info=True)
        time.sleep(5)


def execute_task(app, task_id, ignore_disabled=False, retry_count=0):
    with app.app_context():
        try:
            task = db.session.get(Task, task_id)
            if not task: return
            if getattr(task, 'is_disabled', 0) == 1 and not ignore_disabled: return

            start_time = time.time()
            start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            run_env = get_combined_env(app)

            timeout_str = run_env.get('TASK_TIMEOUT', '1')
            try:
                max_timeout_hours = float(timeout_str)
            except (TypeError, ValueError):
                max_timeout_hours = 1.0
            # [修复 P1-3] TASK_TIMEOUT 为 0 或负数会让所有任务一启动就被强杀，强制钳制
            if max_timeout_hours <= 0:
                max_timeout_hours = 1.0
            max_timeout_seconds = int(max_timeout_hours * 3600)

            filename = task.command.strip()
            # [修复 P0-1] 路径校验失败(返回 None)时，原实现反而放行原始输入，
            # 导致 ../xxx 之类可以执行 scripts 目录之外的文件；此处改为直接拒绝执行。
            target_path = get_safe_path(SCRIPTS_DIR, filename)
            if not target_path:
                reject_msg = f"\n❌ [安全拦截] 任务命令路径非法（禁止目录穿越/绝对路径/越出 scripts 目录）：{filename}\n"
                task.status = 'Error'
                db.session.commit()
                socketio.emit('task_status', {'task_id': task.id, 'status': 'Error', 'duration': 'Rejected'})
                socketio.emit('log_stream', {'task_id': task.id, 'data': reject_msg, 'clear': True})
                return
            safe_rel_path = os.path.relpath(target_path, SCRIPTS_DIR).replace('\\', '/')

            # [修复 P1-7] 任务名可能含路径分隔符导致日志写出到 LOGS_DIR 之外，统一净化
            task_log_dir = os.path.join(LOGS_DIR, sanitize_task_name(task.name))
            os.makedirs(task_log_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file_path = os.path.join(task_log_dir, f"{timestamp}.log")

            cmd_list = ['node', '--require', './ql_env.js', safe_rel_path] if safe_rel_path.endswith('.js') else \
                ['python', safe_rel_path] if safe_rel_path.endswith('.py') else \
                ['bash', safe_rel_path] if safe_rel_path.endswith('.sh') else [safe_rel_path]

            task.status = 'Running'
            task.last_run = start_time_str
            db.session.commit()

            start_msg = f"==============================================\n" \
                        f"🚀 项目开始执行 | 时间: {start_time_str}\n" \
                        f"👉 执行指令: {' '.join(cmd_list)}\n" \
                        f"👉 自动诊断重试次数: {retry_count}\n" \
                        f"==============================================\n\n"

            socketio.emit('task_status', {'task_id': task.id, 'status': 'Running', 'last_run': task.last_run})
            socketio.emit('log_stream', {'task_id': task.id, 'data': start_msg, 'clear': True})

            need_retry = False
            max_auto_retry = 6

            with open(log_file_path, 'w', encoding='utf-8') as f:
                f.write(start_msg)
                f.flush()

                def write_log(msg):
                    f.write(msg)
                    f.flush()
                    socketio.emit('log_stream', {'task_id': task.id, 'data': msg})

                full_output_parts = []

                try:
                    process = subprocess.Popen(cmd_list, shell=False, env=run_env, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, cwd=SCRIPTS_DIR, text=True, bufsize=1,
                                               encoding='utf-8', errors='replace', **SUBPROCESS_KWARGS)
                    running_processes[task_id] = process
                    start_timeout_watcher()

                    # [修复 P2-13] 超时监控统一注册，由共享扫描线程检查（原先每任务一线程 sleep）
                    task_start_times[task_id] = {
                        'process': process,
                        'deadline': start_time + max_timeout_seconds,
                        'log_path': log_file_path,
                        'max_hours': max_timeout_hours
                    }

                    log_buffer = []
                    last_emit_time = time.time()

                    for line in process.stdout:
                        full_output_parts.append(line)
                        f.write(line)
                        log_buffer.append(line)

                        now_ts = time.time()
                        if len(log_buffer) >= 30 or (now_ts - last_emit_time) >= 0.5:
                            chunk = ''.join(log_buffer)
                            f.flush()
                            socketio.emit('log_stream', {'task_id': task.id, 'data': chunk})
                            log_buffer = []
                            last_emit_time = now_ts

                    if log_buffer:
                        chunk = ''.join(log_buffer)
                        f.flush()
                        socketio.emit('log_stream', {'task_id': task.id, 'data': chunk})
                        
                    process.wait()
                    running_processes.pop(task_id, None)
                    task_start_times.pop(task_id, None)
                    returncode = process.returncode
                    
                except Exception as proc_e:
                    err_trace = traceback.format_exc()
                    full_output_parts.append(err_trace)
                    err_msg = f"\n[核心崩溃] 进程启动发生致命异常:\n{err_trace}\n"
                    f.write(err_msg)
                    socketio.emit('log_stream', {'task_id': task.id, 'data': err_msg})
                    returncode = -99

                duration = round(time.time() - start_time, 2)
                output_text = ''.join(full_output_parts)

                diagnosis = diagnose_task_issue(app, output_text, returncode, duration)
                current_fix_key = f"{diagnosis.get('pkg_type')}:{diagnosis.get('package')}" if diagnosis and diagnosis.get('package') else None
                # [修复 P2-7] 修复去重标记改用模块级字典（原先挂在函数属性上，重启丢失且残留）
                repeated_same_fix = last_auto_fixes.get(task_id) == current_fix_key if current_fix_key else False
                if diagnosis:
                    if diagnosis.get('auto_fix') and retry_count < max_auto_retry and not repeated_same_fix:
                        write_log("\n================ 智能诊断开始 ================\n")
                        write_log(f"🔍 已识别问题: {diagnosis.get('title')}\n")
                        write_log(f"📌 可能原因: {diagnosis.get('reason')}\n")
                        write_log(f"🛠️ 自动修复: 准备安装依赖 {diagnosis.get('package')}\n")
                        write_log("==============================================\n\n")
                        last_auto_fixes[task_id] = current_fix_key
                        install_ok, install_msg = run_dependency_install_sync(
                            app,
                            diagnosis.get('package'),
                            diagnosis.get('pkg_type'),
                            log_writer=write_log
                        )

                        if install_ok:
                            write_log(f"\n✅ 自动修复成功，已安装依赖: {diagnosis.get('package')}\n")
                            write_log(f"🔁 系统将自动重新运行任务，当前为第 {retry_count + 1} 次自动修复重跑...\n")
                            need_retry = True
                        else:
                            write_log(f"\n❌ 自动修复失败，依赖安装未成功: {diagnosis.get('package')}\n")
                            if install_msg:
                                write_log(f"📄 安装输出摘要:\n{install_msg[-2000:] if len(install_msg) > 2000 else install_msg}\n")
                            write_diagnosis_message(write_log, diagnosis)
                    else:
                        write_diagnosis_message(write_log, diagnosis)

                end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                end_msg = f"\n==============================================\n" \
                          f"✅ 项目执行完毕 | 时间: {end_time_str}\n" \
                          f"🛑 退出码: {returncode}\n" \
                          f"==============================================\n"
                          
                if (time.time() - start_time) < 1.5 and returncode == 0 and os.path.getsize(log_file_path) < 500:
                    end_msg += f"💡 [提示] 脚本瞬间执行完毕且无输出。可能原因：\n1. 面板环境变量(如 JD_COOKIE)缺失或被禁用。\n2. 脚本依赖的其他环境条件未满足而触发了静默 return。\n"

                if need_retry:
                    end_msg += "🤖 [智能修复] 已完成自动修复，准备进入自动重跑流程。\n"

                f.write(end_msg)
                socketio.emit('log_stream', {'task_id': task.id, 'data': end_msg})

            current_task = db.session.get(Task, task_id)
            if current_task and current_task.status == 'Running':
                current_task.status = 'Idle'
                current_task.last_duration = f"{duration}s"
                db.session.commit()
                socketio.emit('task_status', {'task_id': task.id, 'status': 'Idle', 'duration': f"{duration}s"})

            if need_retry:
                time.sleep(1)
                execute_task(app, task_id, True, retry_count + 1)
                return

        except Exception as e:
            err_trace = traceback.format_exc()
            running_processes.pop(task_id, None)
            task_start_times.pop(task_id, None)
            socketio.emit('log_stream', {'task_id': task_id, 'data': f"\n❌ 错误终止:\n{err_trace}\n"})
            current_task = db.session.get(Task, task_id)
            if current_task:
                current_task.status = 'Error'
                current_task.last_duration = "Failed"
                db.session.commit()
                socketio.emit('task_status', {'task_id': task_id, 'status': 'Error', 'duration': 'Failed'})
        finally:
            emit_queue_status()
            db.session.remove()


def execute_subscription(app, sub_id, ignore_disabled=False):
    with app.app_context():
        stream_id = f"sub_{sub_id}"
        try:
            sub = db.session.get(Subscription, sub_id)
            if not sub: return
            if getattr(sub, 'is_disabled', 0) == 1 and not ignore_disabled: return

            # [修复 P1-7] 别名含路径符号会导致 git clone 目标逃逸 SCRIPTS_DIR，执行前严格校验
            if not validate_alias(sub.alias or ''):
                msg = "❌ 订阅别名包含非法字符（仅允许字母、数字、下划线、中划线），请编辑订阅后重试\n"
                socketio.emit('log_stream', {'task_id': stream_id, 'data': msg, 'clear': True})
                sub.status = 'Error'
                db.session.commit()
                socketio.emit('sub_status', {'sub_id': sub.id, 'status': 'Error'})
                return

            # [加固 P2-15] 订阅地址协议白名单：
            # 单文件下载拒绝 file:// 等（防任意本地文件读取进 scripts）；git 仓库拒绝 ext:: 传输（防命令执行）与 file/ftp
            sub_url = str(sub.url or '').strip()
            sub_url_low = sub_url.lower()
            if sub.type == 'single_file':
                if not (sub_url_low.startswith('http://') or sub_url_low.startswith('https://')):
                    raise Exception("订阅地址协议非法：单文件订阅仅允许 http/https 地址")
            else:
                if sub_url_low.startswith(('ext::', 'file:', 'ftp:')):
                    raise Exception("订阅地址协议非法：仓库地址不允许使用 ext::/file/ftp 传输")

            start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            sub.status = 'Running'
            sub.last_run = start_time_str
            db.session.commit()

            sub_log_dir = os.path.join(LOGS_DIR, 'subscriptions')
            os.makedirs(sub_log_dir, exist_ok=True)
            log_file_path = os.path.join(sub_log_dir, f"sub_{sub.id}.log")

            start_msg = f"==============================================\n" \
                        f"🚀 开始执行订阅任务 | 时间: {start_time_str}\n" \
                        f"👉 订阅名称: {sub.name}\n" \
                        f"👉 目标地址: {sub.url}\n" \
                        f"==============================================\n\n"

            socketio.emit('sub_status', {'sub_id': sub.id, 'status': 'Running', 'last_run': sub.last_run})
            socketio.emit('log_stream', {'task_id': stream_id, 'data': start_msg, 'clear': True})

            with open(log_file_path, 'w', encoding='utf-8') as log_f:
                log_f.write(start_msg)
                
                def write_log(msg):
                    log_f.write(msg)
                    log_f.flush()
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': msg})

                run_env = get_combined_env(app)

                if sub.type == 'single_file':
                    target_dir = os.path.join(SCRIPTS_DIR, 'single_scripts')
                    os.makedirs(target_dir, exist_ok=True)
                    orig_name = sub.url.split('/')[-1] or 'script.js'
                    ext = orig_name.split('.')[-1] if '.' in orig_name else 'js'
                    filename = f"{sub.alias}.{ext}"
                    filepath = os.path.join(target_dir, filename)

                    write_log(f"⬇️ 开始下载单文件: {orig_name} -> 统一保存为 {filename}\n")
                    req = urllib.request.Request(sub.url, headers={'User-Agent': 'Mozilla/5.0'})
                    # [修复 P2-3] 下载加 50MB 上限，防止异常 URL 撑爆内存
                    max_download_bytes = 50 * 1024 * 1024
                    with urllib.request.urlopen(req, timeout=30) as res:
                        data = res.read(max_download_bytes + 1)
                    if len(data) > max_download_bytes:
                        raise Exception("单文件下载超过 50MB 上限，已中止（如确需下载请改用仓库订阅）")
                    with open(filepath, 'wb') as f:
                        f.write(data)
                    write_log(f"✅ 下载完成\n")
                    files_to_process = [filename]
                    current_tasks = Task.query.filter(Task.command.like(f"single_scripts/{sub.alias}.%")).all()
                else:
                    target_dir = os.path.join(SCRIPTS_DIR, sub.alias)
                    if not os.path.exists(os.path.join(target_dir, '.git')):
                        write_log(f"📦 开始克隆仓库: {sub.url}\n")
                        cmd = ['git', 'clone']
                        if sub.branch:
                            cmd.extend(['-b', sub.branch])
                        cmd.extend(['--', sub.url, sub.alias])
                        process = subprocess.Popen(cmd, cwd=SCRIPTS_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=run_env, **SUBPROCESS_KWARGS)
                    else:
                        write_log(f"📦 仓库已存在，开始拉取最新代码...\n")
                        cmd_fetch = ['git', 'fetch', '--all']
                        fetch_result = subprocess.run(cmd_fetch, cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=run_env, **SUBPROCESS_KWARGS)
                        # [修复 P2-4] fetch 失败时中止，避免后续 reset 到过期的 origin
                        if fetch_result.returncode != 0:
                            raise Exception(f"git fetch 失败（退出码 {fetch_result.returncode}）：{fetch_result.stdout[-500:] if fetch_result.stdout else ''}")

                        cmd_reset = ['git', 'reset', '--hard', f"origin/{sub.branch if sub.branch else 'HEAD'}"]
                        process = subprocess.Popen(cmd_reset, cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=run_env, **SUBPROCESS_KWARGS)

                    for line in process.stdout:
                        write_log(line)
                    process.wait()

                    if process.returncode != 0:
                        raise Exception(f"Git 操作失败，退出码: {process.returncode}")

                    apply_custom_overrides_for_subscription(sub, target_dir, write_log)

                    files_to_process = []
                    for root, dirs, files in os.walk(target_dir):
                        # [加固 P3-24] 按路径分量判断，'my.github' 等合法目录不再被误跳过
                        walk_parts = root.replace('\\', '/').split('/')
                        if '.git' in walk_parts: continue
                        for f in files:
                            rel_path = os.path.relpath(os.path.join(root, f), target_dir).replace('\\', '/')
                            files_to_process.append(rel_path)
                            
                    current_tasks = Task.query.filter(Task.command.like(f"{sub.alias}/%")).all()

                write_log(f"\n🔍 开始根据规则过滤文件...\n")

                # [修复 P2-2] 过滤规则直接 re.compile 会在写错正则时让整个订阅失败且无提示；
                # 预校验并明确报出是哪一条规则非法。非法规则直接中止本次同步，
                # 避免 matched_files 为空时 auto_del 把已有任务全部误删。
                def _try_compile(rule_name, pattern):
                    if not pattern:
                        return None
                    try:
                        return re.compile(pattern)
                    except re.error as e:
                        raise Exception(f"过滤规则[{rule_name}]的正则表达式非法: {pattern} （{e}）")

                ext_pattern = _try_compile('扩展名', rf"\.({sub.extensions})$") if sub.extensions else None
                white_pattern = _try_compile('白名单', sub.whitelist) if sub.whitelist else None
                black_pattern = _try_compile('黑名单', sub.blacklist) if sub.blacklist else None
                depend_pattern = _try_compile('依赖文件', sub.depend_file) if sub.depend_file else None

                matched_files = []
                for f in files_to_process:
                    if not f.endswith(('.js', '.py', '.sh')):
                        continue
                    if depend_pattern and depend_pattern.search(f):
                        continue
                    if ext_pattern and not ext_pattern.search(f):
                        continue
                    if white_pattern and not white_pattern.search(f):
                        continue
                    if black_pattern and black_pattern.search(f):
                        continue
                    matched_files.append(f)

                write_log(f"✅ 过滤完成，共匹配到 {len(matched_files)} 个任务脚本\n\n")

                existing_commands = {t.command: t for t in current_tasks}
                processed_commands = set()

                from backend.services.scheduler_service import add_job_to_scheduler

                for f in matched_files:
                    filepath = os.path.join(target_dir, f)
                    command = f"single_scripts/{f}" if sub.type == 'single_file' else f"{sub.alias}/{f}"
                    processed_commands.add(command)
                    
                    script_name, script_cron = parse_script_meta(filepath, os.path.splitext(os.path.basename(f))[0])
                    cron_to_use = script_cron if script_cron else (sub.cron if sub.cron else '0 0 * * *')
                    
                    if command in existing_commands:
                        t = existing_commands[command]
                        updated = False
                        if t.name != script_name:
                            t.name = script_name
                            updated = True
                        if t.cron != cron_to_use:
                            t.cron = cron_to_use
                            updated = True
                        if t.source_type != 'subscription':
                            t.source_type = 'subscription'
                            updated = True
                        if t.source_key != sub.alias:
                            t.source_key = sub.alias
                            updated = True
                        if t.source_name != sub.name:
                            t.source_name = sub.name
                            updated = True
                        if updated:
                            db.session.commit()
                            add_job_to_scheduler(app, t)
                            write_log(f"🔄 更新任务: {script_name} ({command})\n")

                        # [修复 P2-22] TaskView 创建/同步逻辑统一收敛到 ensure_task_view
                        ensure_task_view(sub.name, sub.alias, 'subscription')
                        task_view = TaskView.query.filter_by(source_key=sub.alias).first()
                        if task_view and task_view.name != sub.name:
                            task_view.name = sub.name
                            db.session.commit()
                    else:
                        if sub.auto_add == 1:
                            new_t = Task(
                                name=script_name,
                                command=command,
                                cron=cron_to_use,
                                status='Idle',
                                source_type='subscription',
                                source_key=sub.alias,
                                source_name=sub.name
                            )
                            db.session.add(new_t)
                            db.session.commit()

                            ensure_task_view(sub.name, sub.alias, 'subscription')

                            add_job_to_scheduler(app, new_t)
                            write_log(f"➕ 新增任务: {script_name} ({command})\n")

                if sub.auto_del == 1:
                    for cmd, t in existing_commands.items():
                        if cmd not in processed_commands:
                            if scheduler.get_job(f"task_{t.id}"): 
                                scheduler.remove_job(f"task_{t.id}")
                            db.session.delete(t)
                            write_log(f"➖ 删除失效任务: {t.name} ({cmd})\n")
                    db.session.commit()

                end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                end_msg = f"\n==============================================\n" \
                          f"✅ 订阅执行完毕 | 时间: {end_time_str}\n" \
                          f"==============================================\n"
                write_log(end_msg)

            current_sub = db.session.get(Subscription, sub_id)
            if current_sub and current_sub.status == 'Running':
                current_sub.status = 'Idle'
                db.session.commit()
                socketio.emit('sub_status', {'sub_id': sub.id, 'status': 'Idle'})

        except Exception as e:
            socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n❌ 错误: {str(e)}\n"})
            current_sub = db.session.get(Subscription, sub_id)
            if current_sub:
                current_sub.status = 'Error'
                db.session.commit()
                socketio.emit('sub_status', {'sub_id': sub.id, 'status': 'Error'})
        finally:
            db.session.remove()


def execute_dependency_cmd(app, dep_id, action):
    with app.app_context():
        try:
            dep = db.session.get(Dependency, dep_id)
            if not dep: return

            # [修复 P1-1] pip --target 安装的包，pip uninstall 不作用于目标目录、文件全部残留；
            # 卸载改为直接解析 dist-info/RECORD 删除 PYTHON_DIR 中的包文件。
            if dep.pkg_type == 'pip' and action == 'uninstall':
                log_file_path = get_dependency_log_path(dep.id, dep.name)
                stream_id = f"dep_{dep.id}"
                socketio.emit('log_stream', {'task_id': stream_id, 'data': "🚀 开始卸载 pip 依赖 | 直接移除依赖目录中的包文件\n", 'clear': True})
                removed = uninstall_pip_target_package(dep.name)
                with open(log_file_path, 'w', encoding='utf-8') as f:
                    if removed:
                        msg = f"✅ 依赖 [{dep.name}] 已从依赖目录移除（清理 {removed} 个条目）\n"
                    else:
                        msg = "⚠️ 未在依赖目录中找到该包的安装文件（可能未安装成功或已被手动删除）\n"
                    f.write(msg)
                socketio.emit('log_stream', {'task_id': stream_id, 'data': msg})
                db.session.delete(dep)
                db.session.commit()
                socketio.emit('dep_status', {'id': dep.id, 'status': 'Deleted'})
                return

            node_mirror = SystemConfig.query.filter_by(key='node_mirror').first()
            python_mirror = SystemConfig.query.filter_by(key='python_mirror').first()
            linux_mirror = SystemConfig.query.filter_by(key='linux_mirror').first()

            # [修复 P1-2] 依赖日志文件名统一走 sanitize（scoped npm 包名含 / 会导致写入失败）
            log_file_path = get_dependency_log_path(dep.id, dep.name)
            stream_id = f"dep_{dep.id}"

            run_cwd = SCRIPTS_DIR
            # [加固 P3-22] 依赖名统一校验（拒绝 '-' 开头参数注入）
            if not validate_package_name(dep.name, dep.pkg_type):
                socketio.emit('log_stream', {'task_id': stream_id, 'data': "❌ 依赖名非法，已拒绝执行\n"})
                logger.warning("依赖名非法已拒绝执行: %s (%s)", dep.name, dep.pkg_type)
                return

            # [加固 P2-16] npm/pip 命令改为 shell=False 参数列表（路径含空格也安全），
            # 镜像源仅接受合法 http(s) 地址，防止配置被篡改后注入命令
            cmd_list = None
            if dep.pkg_type == 'npm':
                run_cwd = NODE_DIR
                cmd_list = ['npm', action, dep.name]
                if action == 'install' and node_mirror and node_mirror.value:
                    mirror = sanitize_mirror_url(node_mirror.value)
                    if mirror:
                        cmd_list += ['--registry', mirror]
            elif dep.pkg_type == 'pip':
                run_cwd = PYTHON_DIR
                cmd_list = ['pip', 'install', '--target', PYTHON_DIR, dep.name]
                if python_mirror and python_mirror.value:
                    mirror = sanitize_mirror_url(python_mirror.value)
                    if mirror:
                        cmd_list += ['-i', mirror]
            else:
                run_cwd = LINUX_DIR
                cmd = ""
                if action == 'install' and linux_mirror and linux_mirror.value:
                    # apt 分支保持 shell 串联（sed && apt-get），host 仅允许域名字符
                    host = linux_mirror.value.replace('https://', '').replace('http://', '').strip('/')
                    if not re.match(r'^[A-Za-z0-9\.\-]+$', host):
                        socketio.emit('log_stream', {'task_id': stream_id, 'data': "❌ 镜像源地址非法，已忽略该配置\n"})
                        host = ''
                    if host:
                        cmd += f"sed -i 's/archive.ubuntu.com/{host}/g' /etc/apt/sources.list && apt-get update && "
                cmd += f"apt-get {'remove' if action == 'uninstall' else 'install'} -y {dep.name}"

            cmd = ' '.join(cmd_list) if cmd_list is not None else cmd

            start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            start_msg = f"==============================================\n" \
                        f"🚀 开始{'安装' if action=='install' else '卸载'}依赖 | 时间: {start_time_str}\n" \
                        f"👉 执行指令: {cmd}\n" \
                        f"==============================================\n\n"

            socketio.emit('log_stream', {'task_id': stream_id, 'data': start_msg, 'clear': True})
            with open(log_file_path, 'w', encoding='utf-8') as f:
                f.write(start_msg)

                # [加固 P2-16] npm/pip 已是无 shell 的参数列表；apt 分支（含内部拼接校验）仍走 shell
                popen_cmd = cmd_list if cmd_list is not None else cmd
                popen_shell = cmd_list is None
                process = subprocess.Popen(popen_cmd, shell=popen_shell, env=get_combined_env(app), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=run_cwd, text=True, bufsize=1,
                                           encoding='utf-8', errors='replace', **SUBPROCESS_KWARGS)
                for line in process.stdout:
                    f.write(line)
                    f.flush()
                    # [优化] 移除逐行 os.fsync（每行强制刷盘拖慢安装输出），flush 足够
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': line})
                process.wait()
                
                end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                end_msg = f"\n==============================================\n" \
                          f"✅ 依赖{'安装' if action=='install' else '卸载'}结束 | 时间: {end_time_str}\n" \
                          f"🛑 退出码: {process.returncode}\n" \
                          f"==============================================\n"
                
                f.write(end_msg)
                socketio.emit('log_stream', {'task_id': stream_id, 'data': end_msg})

            current_dep = db.session.get(Dependency, dep_id)
            if current_dep:
                if action == 'install':
                    current_dep.status = 'Installed' if process.returncode == 0 else 'Error'
                    db.session.commit()
                    socketio.emit('dep_status', {'id': current_dep.id, 'status': current_dep.status})
                elif action == 'uninstall':
                    db.session.delete(current_dep)
                    db.session.commit()
                    socketio.emit('dep_status', {'id': current_dep.id, 'status': 'Deleted'})
        except Exception as e:
            # [修复 P2-1] 依赖执行异常原先被完全吞掉，状态会一直卡在 Installing/Uninstalling
            logger.error("依赖命令执行异常: id=%s action=%s (%s)", dep_id, action, e, exc_info=True)
            try:
                stuck = db.session.get(Dependency, dep_id)
                if stuck and stuck.status in ('Installing', 'Uninstalling'):
                    stuck.status = 'Error'
                    db.session.commit()
                    socketio.emit('dep_status', {'id': stuck.id, 'status': 'Error'})
            except Exception:
                db.session.rollback()
        finally:
            db.session.remove()


def execute_debug(app, filename, stream_id):
    with app.app_context():
        try:
            target_path = get_safe_path(SCRIPTS_DIR, filename)
            if not target_path: raise Exception("非法路径")

            run_env = get_combined_env(app)

            # [加固 P3-20] 调试进程纳入统一超时监控（沿用全局 TASK_TIMEOUT 配置）
            timeout_str = run_env.get('TASK_TIMEOUT', '1')
            try:
                max_timeout_hours = float(timeout_str)
            except (TypeError, ValueError):
                max_timeout_hours = 1.0
            if max_timeout_hours <= 0:
                max_timeout_hours = 1.0

            cmd_list = ['node', '--require', './ql_env.js', target_path] if target_path.endswith('.js') else \
                ['python', target_path] if target_path.endswith('.py') else \
                ['bash', target_path] if target_path.endswith('.sh') else [target_path]

            socketio.emit('log_stream',
                          {'task_id': stream_id, 'data': f"🚀 开始调试执行: {' '.join(cmd_list)}\n", 'clear': True})

            process = subprocess.Popen(cmd_list, shell=False, env=run_env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, cwd=SCRIPTS_DIR, text=True, bufsize=1,
                                       encoding='utf-8', errors='replace', **SUBPROCESS_KWARGS)
            debug_processes[stream_id] = process
            start_timeout_watcher()
            task_start_times[stream_id] = {
                'process': process,
                'deadline': time.time() + int(max_timeout_hours * 3600),
                'log_path': None,
                'max_hours': max_timeout_hours
            }

            for line in process.stdout:
                socketio.emit('log_stream', {'task_id': stream_id, 'data': line})
            process.wait()
            debug_processes.pop(stream_id, None)
            task_start_times.pop(stream_id, None)
            socketio.emit('log_stream',
                          {'task_id': stream_id, 'data': f"\n✅ 调试执行结束，退出码: {process.returncode}\n"})
        except Exception as e:
            debug_processes.pop(stream_id, None)
            task_start_times.pop(stream_id, None)
            socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n❌ 调试出错: {str(e)}\n"})