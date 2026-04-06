import os
import time
import re
import ast
import tempfile
import traceback
import subprocess
import threading
import urllib.request
import shutil
from datetime import datetime
from backend.models import db, Task, Env, Dependency, SystemConfig, Subscription, TaskView
from backend.extensions import socketio, scheduler
from backend.runtime import running_processes, debug_processes, SUBPROCESS_KWARGS, queued_tasks, task_queue
from backend.core.paths import SCRIPTS_DIR, LOGS_DIR, NODE_DIR, PYTHON_DIR, LINUX_DIR, CUSTOM_OVERRIDE_DIR
from backend.core.security import get_safe_path
from backend.core.env_manager import get_combined_env
from backend.core.dependency_manager import run_dependency_install_sync, normalize_python_package_name
from backend.core.task_helpers import parse_script_meta

_queue_lock = threading.Lock()
_queue_worker_started = False


def emit_queue_status():
    try:
        socketio.emit('queue_status', {
            'queued_count': len(task_queue)
        })
    except:
        pass


def get_task_concurrency_limit():
    try:
        cfg = SystemConfig.query.filter_by(key='task_concurrency').first()
        if cfg and str(cfg.value).isdigit():
            value = int(cfg.value)
            return value if value > 0 else 5
    except:
        pass
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
        task = Task.query.get(task_id)
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
                limit = get_task_concurrency_limit()

                while len(running_processes) < limit and task_queue:
                    task_id = task_queue.pop(0)
                    queued_info = queued_tasks.pop(task_id, None)
                    emit_queue_status()
                    if not queued_info:
                        continue

                    task = Task.query.get(task_id)
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
        except:
            pass

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
            except:
                pass
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


def execute_task(app, task_id, ignore_disabled=False, retry_count=0):
    with app.app_context():
        try:
            task = Task.query.get(task_id)
            if not task: return
            if getattr(task, 'is_disabled', 0) == 1 and not ignore_disabled: return

            start_time = time.time()
            start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            run_env = get_combined_env(app)

            timeout_str = run_env.get('TASK_TIMEOUT', '1')
            try:
                max_timeout_hours = float(timeout_str)
            except:
                max_timeout_hours = 1.0
            max_timeout_seconds = int(max_timeout_hours * 3600)

            filename = task.command.strip()
            target_path = get_safe_path(SCRIPTS_DIR, filename)
            if not target_path:
                safe_rel_path = filename
            else:
                safe_rel_path = target_path

            task_log_dir = os.path.join(LOGS_DIR, task.name)
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

                    def timeout_monitor():
                        time.sleep(max_timeout_seconds)
                        if task_id in running_processes and running_processes[task_id] == process:
                            if process.poll() is None:
                                try:
                                    process.kill()
                                    warn_msg = f"\n❌ 任务执行超过最大设定时长 ({max_timeout_hours} 小时)，已被系统强制终止！\n"
                                    socketio.emit('log_stream', {'task_id': task_id, 'data': warn_msg})
                                    f.write(warn_msg)
                                except:
                                    pass

                    threading.Thread(target=timeout_monitor, daemon=True).start()

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
                last_fix_key = f"_last_fix_{task_id}"
                current_fix_key = f"{diagnosis.get('pkg_type')}:{diagnosis.get('package')}" if diagnosis and diagnosis.get('package') else None
                repeated_same_fix = getattr(execute_task, last_fix_key, None) == current_fix_key if current_fix_key else False
                if diagnosis:
                    if diagnosis.get('auto_fix') and retry_count < max_auto_retry and not repeated_same_fix:
                        write_log("\n================ 智能诊断开始 ================\n")
                        write_log(f"🔍 已识别问题: {diagnosis.get('title')}\n")
                        write_log(f"📌 可能原因: {diagnosis.get('reason')}\n")
                        write_log(f"🛠️ 自动修复: 准备安装依赖 {diagnosis.get('package')}\n")
                        write_log("==============================================\n\n")
                        setattr(execute_task, last_fix_key, current_fix_key)
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

            current_task = Task.query.get(task_id)
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
            socketio.emit('log_stream', {'task_id': task_id, 'data': f"\n❌ 错误终止:\n{err_trace}\n"})
            current_task = Task.query.get(task_id)
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
        try:
            sub = Subscription.query.get(sub_id)
            if not sub: return
            if getattr(sub, 'is_disabled', 0) == 1 and not ignore_disabled: return

            start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stream_id = f"sub_{sub.id}"
            
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
                    with urllib.request.urlopen(req, timeout=30) as res, open(filepath, 'wb') as f:
                        f.write(res.read())
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
                        subprocess.run(cmd_fetch, cwd=target_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=run_env, **SUBPROCESS_KWARGS)
                        
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
                        if '.git' in root: continue
                        for f in files:
                            rel_path = os.path.relpath(os.path.join(root, f), target_dir).replace('\\', '/')
                            files_to_process.append(rel_path)
                            
                    current_tasks = Task.query.filter(Task.command.like(f"{sub.alias}/%")).all()

                write_log(f"\n🔍 开始根据规则过滤文件...\n")
                
                ext_pattern = re.compile(rf"\.({sub.extensions})$") if sub.extensions else None
                white_pattern = re.compile(sub.whitelist) if sub.whitelist else None
                black_pattern = re.compile(sub.blacklist) if sub.blacklist else None
                depend_pattern = re.compile(sub.depend_file) if sub.depend_file else None

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

                        task_view = TaskView.query.filter_by(source_key=sub.alias).first()
                        if not task_view:
                            max_order = db.session.query(db.func.max(TaskView.sort_order)).scalar() or 0
                            db.session.add(TaskView(
                                name=sub.name,
                                source_key=sub.alias,
                                source_type='subscription',
                                is_visible=0,
                                is_system=0,
                                sort_order=max_order + 1
                            ))
                            db.session.commit()
                        else:
                            if task_view.name != sub.name:
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

                            task_view = TaskView.query.filter_by(source_key=sub.alias).first()
                            if not task_view:
                                max_order = db.session.query(db.func.max(TaskView.sort_order)).scalar() or 0
                                db.session.add(TaskView(
                                    name=sub.name,
                                    source_key=sub.alias,
                                    source_type='subscription',
                                    is_visible=0,
                                    is_system=0,
                                    sort_order=max_order + 1
                                ))
                                db.session.commit()

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

            current_sub = Subscription.query.get(sub_id)
            if current_sub and current_sub.status == 'Running':
                current_sub.status = 'Idle'
                db.session.commit()
                socketio.emit('sub_status', {'sub_id': sub.id, 'status': 'Idle'})

        except Exception as e:
            socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n❌ 错误: {str(e)}\n"})
            current_sub = Subscription.query.get(sub_id)
            if current_sub:
                current_sub.status = 'Error'
                db.session.commit()
                socketio.emit('sub_status', {'sub_id': sub.id, 'status': 'Error'})
        finally:
            db.session.remove()


def execute_dependency_cmd(app, dep_id, action):
    with app.app_context():
        try:
            dep = Dependency.query.get(dep_id)
            if not dep: return

            node_mirror = SystemConfig.query.filter_by(key='node_mirror').first()
            python_mirror = SystemConfig.query.filter_by(key='python_mirror').first()
            linux_mirror = SystemConfig.query.filter_by(key='linux_mirror').first()

            dep_log_dir = os.path.join(LOGS_DIR, 'dependencies')
            os.makedirs(dep_log_dir, exist_ok=True)
            log_file_path = os.path.join(dep_log_dir, f"dep_{dep.id}_{dep.name}.log")
            stream_id = f"dep_{dep.id}"

            run_cwd = SCRIPTS_DIR
            if dep.pkg_type == 'npm':
                run_cwd = NODE_DIR
                cmd = f"npm {action} {dep.name}"
                if action == 'install' and node_mirror and node_mirror.value:
                    cmd += f" --registry={node_mirror.value}"
            elif dep.pkg_type == 'pip':
                run_cwd = PYTHON_DIR
                if action == 'install':
                    cmd = f"pip install --target={PYTHON_DIR} {dep.name}"
                    if python_mirror and python_mirror.value: cmd += f" -i {python_mirror.value}"
                else:
                    cmd = f"pip uninstall -y {dep.name}"
            else:
                run_cwd = LINUX_DIR
                cmd = ""
                if action == 'install' and linux_mirror and linux_mirror.value:
                    host = linux_mirror.value.replace('https://', '').replace('http://', '').strip('/')
                    cmd += f"sed -i 's/archive.ubuntu.com/{host}/g' /etc/apt/sources.list && apt-get update && "
                cmd += f"apt-get {'remove' if action == 'uninstall' else 'install'} -y {dep.name}"

            start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            start_msg = f"==============================================\n" \
                        f"🚀 开始{'安装' if action=='install' else '卸载'}依赖 | 时间: {start_time_str}\n" \
                        f"👉 执行指令: {cmd}\n" \
                        f"==============================================\n\n"

            socketio.emit('log_stream', {'task_id': stream_id, 'data': start_msg, 'clear': True})
            with open(log_file_path, 'w', encoding='utf-8') as f:
                f.write(start_msg)
                
                process = subprocess.Popen(cmd, shell=True, env=get_combined_env(app), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=run_cwd, text=True, bufsize=1,
                                           encoding='utf-8', errors='replace', **SUBPROCESS_KWARGS)
                for line in process.stdout:
                    f.write(line)
                    f.flush()               
                    os.fsync(f.fileno())    
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': line})
                process.wait()
                
                end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                end_msg = f"\n==============================================\n" \
                          f"✅ 依赖{'安装' if action=='install' else '卸载'}结束 | 时间: {end_time_str}\n" \
                          f"🛑 退出码: {process.returncode}\n" \
                          f"==============================================\n"
                
                f.write(end_msg)
                socketio.emit('log_stream', {'task_id': stream_id, 'data': end_msg})

            current_dep = Dependency.query.get(dep_id)
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
            pass
        finally:
            db.session.remove()


def execute_debug(app, filename, stream_id):
    with app.app_context():
        try:
            target_path = get_safe_path(SCRIPTS_DIR, filename)
            if not target_path: raise Exception("非法路径")
            
            run_env = get_combined_env(app)
            
            cmd_list = ['node', '--require', './ql_env.js', target_path] if target_path.endswith('.js') else \
                ['python', target_path] if target_path.endswith('.py') else \
                ['bash', target_path] if target_path.endswith('.sh') else [target_path]

            socketio.emit('log_stream',
                          {'task_id': stream_id, 'data': f"🚀 开始调试执行: {' '.join(cmd_list)}\n", 'clear': True})

            process = subprocess.Popen(cmd_list, shell=False, env=run_env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, cwd=SCRIPTS_DIR, text=True, bufsize=1,
                                       encoding='utf-8', errors='replace', **SUBPROCESS_KWARGS)
            debug_processes[stream_id] = process
            
            for line in process.stdout:
                socketio.emit('log_stream', {'task_id': stream_id, 'data': line})
            process.wait()
            debug_processes.pop(stream_id, None)
            socketio.emit('log_stream',
                          {'task_id': stream_id, 'data': f"\n✅ 调试执行结束，退出码: {process.returncode}\n"})
        except Exception as e:
            debug_processes.pop(stream_id, None)
            socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n❌ 调试出错: {str(e)}\n"})