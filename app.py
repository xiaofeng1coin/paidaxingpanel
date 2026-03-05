import os
import sys
import getpass
import subprocess
import threading
import re
import ast
import tempfile
import zipfile
import io
import urllib.request
import urllib.parse
import json
import time
import base64
import hmac
import struct
import hashlib
import random
import string
import shutil  # 【新增】用于文件复制操作
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename  # 【安全修复】新增：用于防止目录穿越漏洞
from flask_socketio import SocketIO
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from database import db, User, Task, Env, Dependency, LoginSecurity, SystemConfig, LoginLog

BASE_DIR = os.path.abspath(os.path.dirname(__name__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

SCRIPTS_DIR = os.path.join(DATA_DIR, 'scripts')
LOGS_DIR = os.path.join(DATA_DIR, 'logs')
DB_DIR = os.path.join(DATA_DIR, 'db')
CONFIG_DIR = os.path.join(DATA_DIR, 'config')

DEPS_ENV_DIR = os.path.join(DATA_DIR, 'deps_env')
NODE_DIR = os.path.join(DEPS_ENV_DIR, 'nodejs')
PYTHON_DIR = os.path.join(DEPS_ENV_DIR, 'python')
LINUX_DIR = os.path.join(DEPS_ENV_DIR, 'linux')

CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.sh')

os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(NODE_DIR, exist_ok=True)
os.makedirs(PYTHON_DIR, exist_ok=True)
os.makedirs(LINUX_DIR, exist_ok=True)

node_pkg_json = os.path.join(NODE_DIR, 'package.json')
if not os.path.exists(node_pkg_json):
    with open(node_pkg_json, 'w', encoding='utf-8') as f:
        f.write('{"name": "pdx-deps","version": "1.0.0"}')

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write("# 派大星面板 Global Config\n# Format: export KEY=\"VALUE\"\n\n")
        f.write("# 全局任务最大执行时间(小时)，支持小数如0.1\n")
        f.write("export TASK_TIMEOUT=\"1\"\n")
else:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
    if 'TASK_TIMEOUT' not in content:
        with open(CONFIG_FILE, 'a', encoding='utf-8') as f:
            f.write("\n# 全局任务最大执行时间(小时)，支持小数如0.1\nexport TASK_TIMEOUT=\"1\"\n")
    elif 'export TASK_TIMEOUT="3600"' in content:
        content = content.replace('export TASK_TIMEOUT="3600"', 'export TASK_TIMEOUT="1"')
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write(content)

app = Flask(__name__)
app.secret_key = os.urandom(32)

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(DB_DIR, 'auth.db')}"
app.config['SQLALCHEMY_BINDS'] = {
    'tasks': f"sqlite:///{os.path.join(DB_DIR, 'tasks.db')}",
    'envs': f"sqlite:///{os.path.join(DB_DIR, 'envs.db')}"
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

db.init_app(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
scheduler = BackgroundScheduler()
running_processes = {}

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


def generate_totp_secret():
    return ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567') for _ in range(16))


def verify_totp(secret, code):
    try:
        padding = len(secret) % 8
        if padding != 0:
            secret += '=' * (8 - padding)
        key = base64.b32decode(secret, casefold=True)
        current_time = int(time.time() / 30)
        for i in range(-1, 2):
            msg = struct.pack(">Q", current_time + i)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            o = h[19] & 15
            token = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 1000000
            if f"{token:06d}" == str(code).strip():
                return True
        return False
    except:
        return False


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.context_processor
def inject_global_settings():
    sys_theme = 'auto'
    sys_lang = 'auto'
    last_login = None
    try:
        if current_user.is_authenticated or request.endpoint == 'login':
            configs = {c.key: c.value for c in SystemConfig.query.all()}
            sys_theme = configs.get('theme', 'auto')
            sys_lang = configs.get('language', 'auto')
            if current_user.is_authenticated and 'last_login_info' in session:
                last_login = session.pop('last_login_info', None)
    except:
        pass
    return dict(sys_theme=sys_theme, sys_lang=sys_lang, last_login_info=last_login)


@app.before_request
def global_security_check():
    if not request.path.startswith('/static/'):
        if User.query.count() == 0 and request.endpoint not in ['install']:
            return redirect(url_for('install'))

    session.modified = True

    allowed_direct_endpoints = [
        'login', 'install', 'logout', 'tasks', 'static',
        'scripts_editor', 'scripts_debug', 'api_debug_run', 'logs', 'deps', 'envs', 'config_editor', 'settings'
    ]
    if current_user.is_authenticated and request.method == 'GET' and request.endpoint not in allowed_direct_endpoints:
        referer = request.headers.get("Referer")
        if not referer or request.host not in referer:
            flash("为保证安全，禁止直接输入地址访问未授权子页面，已退回首页。")
            return redirect(url_for('tasks'))


def send_sys_notify(title, content):
    def _send():
        with app.app_context():
            configs = {c.key: str(c.value) for c in SystemConfig.query.all() if c.value is not None}
            notify_type = configs.get('notify_type', 'none')
            if notify_type == 'none':
                return

            env = get_combined_env()
            for k, v in configs.items(): env[k] = v

            if notify_type == 'telegram':
                env['TG_BOT_TOKEN'] = configs.get('TG_BOT_TOKEN', '')
                env['TG_USER_ID'] = configs.get('TG_USER_ID', '')
            elif notify_type == 'dingtalk':
                env['DD_BOT_TOKEN'] = configs.get('DD_BOT_TOKEN', '')
                env['DD_BOT_SECRET'] = configs.get('DD_BOT_SECRET', '')
            elif notify_type == 'pushplus':
                env['PUSH_PLUS_TOKEN'] = configs.get('PUSH_PLUS_TOKEN', '')
            elif notify_type == 'serverchan':
                env['PUSH_KEY'] = configs.get('PUSH_KEY', '')

            sys_notify_js = os.path.join(SCRIPTS_DIR, 'sys_notify.js')
            if os.path.exists(sys_notify_js):
                try:
                    subprocess.run(['node', 'sys_notify.js', title, content], env=env, cwd=SCRIPTS_DIR, timeout=30)
                except Exception as e:
                    pass

    threading.Thread(target=_send, daemon=True).start()


def get_combined_env():
    run_env = os.environ.copy()
    run_env['PYTHONUNBUFFERED'] = '1'

    run_env['NODE_PATH'] = os.path.join(NODE_DIR, 'node_modules')
    existing_pythonpath = run_env.get('PYTHONPATH', '')
    run_env['PYTHONPATH'] = f"{PYTHON_DIR}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else PYTHON_DIR

    with app.app_context():
        # 修复：注入运行时环境也按 position 顺序注入
        for e in Env.query.filter((Env.is_disabled == 0) | (Env.is_disabled == None)).order_by(
                Env.position.asc()).all():
            run_env[e.name] = str(e.value)

        proxy_cfg = SystemConfig.query.filter_by(key='proxy').first()
        if proxy_cfg and proxy_cfg.value:
            run_env['HTTP_PROXY'] = proxy_cfg.value
            run_env['HTTPS_PROXY'] = proxy_cfg.value
            run_env['ALL_PROXY'] = proxy_cfg.value

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('export '):
                    match = re.match(r'^export\s+([A-Za-z0-9_]+)=[\'"]?(.*?)[\'"]?$', line)
                    if match: run_env[match.group(1)] = match.group(2)
    return run_env


def execute_task(task_id):
    with app.app_context():
        try:
            task = Task.query.get(task_id)
            if not task or getattr(task, 'is_disabled', 0) == 1: return

            start_time = time.time()
            run_env = get_combined_env()

            timeout_str = run_env.get('TASK_TIMEOUT', '1')
            try:
                max_timeout_hours = float(timeout_str)
            except:
                max_timeout_hours = 1.0
            max_timeout_seconds = int(max_timeout_hours * 3600)

            task_log_dir = os.path.join(LOGS_DIR, task.name)
            os.makedirs(task_log_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file_path = os.path.join(task_log_dir, f"{timestamp}.log")

            # subprocess 默认使用 shell=False 并以列表形式传参，本身就杜绝了任务命令注入
            filename = task.command.strip()
            cmd_list = ['node', '--require', './ql_env.js', filename] if filename.endswith('.js') else \
                ['python', filename] if filename.endswith('.py') else \
                    ['bash', filename] if filename.endswith('.sh') else [filename]

            task.status = 'Running'
            task.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.session.commit()

            socketio.emit('task_status', {'task_id': task.id, 'status': 'Running', 'last_run': task.last_run})
            socketio.emit('log_stream',
                          {'task_id': task.id, 'data': f"[{datetime.now()}] 🚀 执行: {' '.join(cmd_list)}\n",
                           'clear': True})

            with open(log_file_path, 'w', encoding='utf-8') as f:
                process = subprocess.Popen(cmd_list, shell=False, env=run_env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=SCRIPTS_DIR, text=True, bufsize=1,
                                           encoding='utf-8', errors='replace')
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

                for line in process.stdout:
                    f.write(line)
                    socketio.emit('log_stream', {'task_id': task.id, 'data': line})
                process.wait()
                running_processes.pop(task_id, None)

                if process.returncode is not None and process.returncode != -9:
                    f.write(f"\n✅ 退出码: {process.returncode}\n")
                    socketio.emit('log_stream', {'task_id': task.id, 'data': f"\n✅ 退出码: {process.returncode}\n"})

            duration = round(time.time() - start_time, 2)
            current_task = Task.query.get(task_id)
            if current_task and current_task.status == 'Running':
                current_task.status = 'Idle'
                current_task.last_duration = f"{duration}s"
                db.session.commit()
                socketio.emit('task_status', {'task_id': task.id, 'status': 'Idle', 'duration': f"{duration}s"})

        except Exception as e:
            running_processes.pop(task_id, None)
            socketio.emit('log_stream', {'task_id': task_id, 'data': f"\n❌ 错误: {str(e)}\n"})
            current_task = Task.query.get(task_id)
            if current_task:
                current_task.status = 'Error'
                current_task.last_duration = "Failed"
                db.session.commit()
                socketio.emit('task_status', {'task_id': task_id, 'status': 'Error', 'duration': 'Failed'})
        finally:
            db.session.remove()


def add_job_to_scheduler(task):
    if getattr(task, 'is_disabled', 0) == 1: return
    job_id = f"task_{task.id}"
    if scheduler.get_job(job_id): scheduler.remove_job(job_id)
    try:
        scheduler.add_job(execute_task, CronTrigger.from_crontab(task.cron), args=[task.id], id=job_id)
    except:
        pass


@app.route('/install', methods=['GET', 'POST'])
def install():
    if User.query.count() > 0:
        return redirect(url_for('login'))

    if request.method == 'POST':
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')

        if not username or not password:
            return jsonify({"status": "error", "msg": "账号和密码不能为空"})

        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)

        notify_type = data.get('notify_type', 'none')
        db.session.add(SystemConfig(key='notify_type', value=notify_type))

        if notify_type == 'telegram':
            db.session.add(SystemConfig(key='TG_BOT_TOKEN', value=data.get('TG_BOT_TOKEN', '')))
            db.session.add(SystemConfig(key='TG_USER_ID', value=data.get('TG_USER_ID', '')))
        elif notify_type == 'dingtalk':
            db.session.add(SystemConfig(key='DD_BOT_TOKEN', value=data.get('DD_BOT_TOKEN', '')))
            db.session.add(SystemConfig(key='DD_BOT_SECRET', value=data.get('DD_BOT_SECRET', '')))
        elif notify_type == 'pushplus':
            db.session.add(SystemConfig(key='PUSH_PLUS_TOKEN', value=data.get('PUSH_PLUS_TOKEN', '')))
        elif notify_type == 'serverchan':
            db.session.add(SystemConfig(key='PUSH_KEY', value=data.get('PUSH_KEY', '')))

        db.session.add(LoginSecurity(failed_count=0))
        db.session.commit()
        return jsonify({"status": "success"})

    return render_template('install.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('tasks'))

    sec = LoginSecurity.query.first()
    if not sec:
        sec = LoginSecurity(failed_count=0)
        db.session.add(sec)
        db.session.commit()

    if request.method == 'POST':
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip and ',' in ip: ip = ip.split(',')[0].strip()

        ua_string = request.user_agent.string.lower() if request.user_agent else ""
        if any(kw in ua_string for kw in ['mobile', 'android', 'iphone', 'ipad', 'ipod', 'windows phone']):
            device = "mobile"
        else:
            device = "desktop"

        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def get_address(ip_addr):
            if not ip_addr or ip_addr.startswith("192.") or ip_addr.startswith("10.") or ip_addr.startswith(
                    "172.") or ip_addr == "127.0.0.1" or ip_addr == "::1":
                return "内网IP"
            try:
                req = urllib.request.Request(f"http://ip-api.com/json/{ip_addr}?lang=zh-CN",
                                             headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=3) as res:
                    data = json.loads(res.read().decode('utf-8'))
                    if data.get(
                            'status') == 'success': return f"{data.get('country', '')} {data.get('regionName', '')} {data.get('city', '')}".strip()
            except:
                pass
            return "未知网络"

        address = get_address(ip)

        req_username = request.form.get('username')
        req_password = request.form.get('password')

        if sec.locked_until and datetime.now() < sec.locked_until:
            flash(f"尝试次数过多，请在 {sec.locked_until.strftime('%H:%M:%S')} 后重试")
            log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="被锁定")
            db.session.add(log);
            db.session.commit()
            send_sys_notify("派大星面板-安全告警",
                            f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：账户因多次失败被锁定")
            return render_template('login.html', temp_user=req_username, temp_pwd=req_password)

        user = User.query.filter_by(username=req_username).first()
        if user and check_password_hash(user.password_hash, req_password):

            totp_secret = SystemConfig.query.filter_by(key='totp_secret').first()
            if totp_secret and totp_secret.value:
                totp_code = request.form.get('totp_code')
                if not totp_code:
                    return render_template('login.html', need_2fa=True, temp_user=req_username, temp_pwd=req_password)
                else:
                    if not verify_totp(totp_secret.value, totp_code):
                        sec.failed_count += 1
                        if sec.failed_count >= 5:
                            lock_mins = (sec.failed_count - 4) * 5
                            sec.locked_until = datetime.now() + timedelta(minutes=lock_mins)
                            flash(f"动态验证码多次错误，账号被锁定 {lock_mins} 分钟。")
                        else:
                            flash(f"两步验证码错误！还可以尝试 {5 - sec.failed_count} 次。")
                        db.session.commit()

                        if sec.locked_until and datetime.now() < sec.locked_until:
                            return redirect(url_for('login'))
                        return render_template('login.html', need_2fa=True, temp_user=req_username,
                                               temp_pwd=req_password)

            sec.failed_count = 0
            sec.locked_until = None

            last_log = LoginLog.query.order_by(LoginLog.id.desc()).first()
            if last_log:
                session['last_login_info'] = {
                    'time': last_log.login_time,
                    'ip': last_log.ip,
                    'address': last_log.address,
                    'status': last_log.status
                }

            log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="成功")
            db.session.add(log);
            db.session.commit()

            session.permanent = True
            login_user(user)

            send_sys_notify("派大星面板-登录成功", f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：登录成功")
            return redirect(url_for('tasks'))

        sec.failed_count += 1
        if sec.failed_count >= 5:
            lock_mins = (sec.failed_count - 4) * 5
            sec.locked_until = datetime.now() + timedelta(minutes=lock_mins)
            flash(f"连续登录失败 {sec.failed_count} 次，账号被锁定 {lock_mins} 分钟。")
        else:
            flash(f"账号或密码错误！还有 {5 - sec.failed_count} 次尝试机会。")

        log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="失败")
        db.session.add(log);
        db.session.commit()
        send_sys_notify("派大星面板-登录失败",
                        f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：密码错误（已失败 {sec.failed_count} 次）")

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/api/2fa/generate')
@login_required
def api_2fa_generate():
    secret = generate_totp_secret()
    issuer = "PatrickPanel"
    account = current_user.username
    label = urllib.parse.quote(f"{issuer}:{account}")
    encoded_issuer = urllib.parse.quote(issuer)
    uri = f"otpauth://totp/{label}?secret={secret}&issuer={encoded_issuer}"
    return jsonify({"secret": secret, "uri": uri})


@app.route('/api/2fa/enable', methods=['POST'])
@login_required
def api_2fa_enable():
    secret = request.form.get('secret')
    code = request.form.get('code')
    if verify_totp(secret, code):
        cfg = SystemConfig.query.filter_by(key='totp_secret').first()
        if cfg:
            cfg.value = secret
        else:
            db.session.add(SystemConfig(key='totp_secret', value=secret))
        db.session.commit()
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "msg": "验证码不正确或已过期"})


@app.route('/api/2fa/disable', methods=['POST'])
@login_required
def api_2fa_disable():
    pwd = request.form.get('password')
    if not check_password_hash(current_user.password_hash, pwd):
        return jsonify({"status": "error", "msg": "系统密码验证失败"})

    cfg = SystemConfig.query.filter_by(key='totp_secret').first()
    if cfg:
        db.session.delete(cfg)
        db.session.commit()
    return jsonify({"status": "success"})


@app.route('/')
@login_required
def tasks():
    return render_template('tasks.html', tasks=Task.query.all())


@app.route('/task/add', methods=['POST'])
@login_required
def add_task():
    new_task = Task(name=request.form.get('name'), command=request.form.get('command').strip(),
                    cron=request.form.get('cron'), status='Idle')
    db.session.add(new_task);
    db.session.commit();
    add_job_to_scheduler(new_task)
    return redirect(url_for('tasks'))


@app.route('/task/edit/<int:id>', methods=['POST'])
@login_required
def edit_task(id):
    task = Task.query.get(id)
    if task:
        task.name = request.form.get('name')
        task.command = request.form.get('command').strip()
        task.cron = request.form.get('cron')
        db.session.commit()
        add_job_to_scheduler(task)
    return redirect(url_for('tasks'))


@app.route('/api/task/toggle/<int:id>')
@login_required
def toggle_task(id):
    task = Task.query.get(id)
    if task:
        task.is_disabled = 1 if getattr(task, 'is_disabled', 0) == 0 else 0
        db.session.commit()
        if task.is_disabled == 1:
            if scheduler.get_job(f"task_{task.id}"): scheduler.remove_job(f"task_{task.id}")
        else:
            add_job_to_scheduler(task)
    return redirect(url_for('tasks'))


@app.route('/task/delete/<int:id>')
@login_required
def delete_task(id):
    task = Task.query.get(id)
    if task:
        if id in running_processes:
            try:
                running_processes[id].kill()
            except:
                pass
        if scheduler.get_job(f"task_{task.id}"): scheduler.remove_job(f"task_{task.id}")
        db.session.delete(task);
        db.session.commit()
    return redirect(url_for('tasks'))


@app.route('/api/task/run/<int:id>')
@login_required
def api_run_task(id):
    task = Task.query.get(id)
    if task and getattr(task, 'is_disabled', 0) == 1: return jsonify({"status": "error", "msg": "该任务已被禁用"})
    if id in running_processes: return jsonify({"status": "error", "msg": "运行中"})
    threading.Thread(target=execute_task, args=(id,)).start()
    return jsonify({"status": "success"})


@app.route('/api/task/stop/<int:id>')
@login_required
def api_stop_task(id):
    task = Task.query.get(id)
    if not task: return jsonify({"status": "error"})
    if id in running_processes:
        try:
            running_processes[id].kill()
        except:
            pass
        finally:
            running_processes.pop(id, None)
            socketio.emit('log_stream', {'task_id': id, 'data': f"\n🛑 手动停止\n"})
    task.status = 'Idle';
    db.session.commit();
    socketio.emit('task_status', {'task_id': id, 'status': 'Idle', 'duration': 'Stopped'})
    return jsonify({"status": "success"})


@app.route('/api/task/log/<int:id>')
@login_required
def api_get_task_log(id):
    task = Task.query.get(id)
    if not task: return jsonify({"content": "不存在"})
    task_log_dir = os.path.join(LOGS_DIR, task.name)
    if os.path.exists(task_log_dir):
        files = sorted(os.listdir(task_log_dir), reverse=True)
        if files:
            with open(os.path.join(task_log_dir, files[0]), 'r', encoding='utf-8') as f: return jsonify(
                {"content": f.read()})
    return jsonify({"content": "暂无日志..."})


@app.route('/deps')
@login_required
def deps(): return render_template('deps.html', dependencies=Dependency.query.all())


def execute_dependency_cmd(dep_id, action):
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

            socketio.emit('log_stream', {'task_id': stream_id, 'data': f"🚀 执行: {cmd}\n", 'clear': True})
            with open(log_file_path, 'w', encoding='utf-8') as f:
                process = subprocess.Popen(cmd, shell=True, env=get_combined_env(), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, cwd=run_cwd, text=True, bufsize=1,
                                           encoding='utf-8', errors='replace')
                for line in process.stdout:
                    f.write(line)
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': line})
                process.wait()
            current_dep = Dependency.query.get(dep_id)
            if current_dep:
                if action == 'install':
                    current_dep.status = 'Installed' if process.returncode == 0 else 'Error'
                    db.session.commit()
                    socketio.emit('dep_status', {'id': current_dep.id, 'status': current_dep.status})
                elif action == 'uninstall':
                    if process.returncode == 0:
                        db.session.delete(current_dep);
                        db.session.commit();
                        socketio.emit('dep_status', {'id': current_dep.id, 'status': 'Deleted'})
                    else:
                        current_dep.status = 'Error';
                        db.session.commit();
                        socketio.emit('dep_status', {'id': current_dep.id, 'status': 'Error'})
        except Exception as e:
            pass
        finally:
            db.session.remove()


@app.route('/api/deps/install', methods=['POST'])
@login_required
def api_install_deps():
    pkg_type = request.form.get('type')
    package = request.form.get('package', '').strip()

    # 【安全修复 1：防止命令注入 RCE】
    # 限制包名只能包含：大小写字母、数字、下划线、中划线、点、@ (用于版本号或范围名)、/ (用于范围包)
    # 彻底杜绝使用分号、管道符、反引号等执行服务器毁灭级命令。
    if not package or not re.match(r'^[A-Za-z0-9_\-\.\@\/]+$', package):
        return jsonify({"status": "error", "msg": "依赖包名称包含非法字符，出于安全考虑拒绝执行"})

    dep = Dependency.query.filter_by(name=package, pkg_type=pkg_type).first()
    if not dep:
        dep = Dependency(name=package, pkg_type=pkg_type, status='Installing')
        db.session.add(dep)
    else:
        dep.status = 'Installing'
    db.session.commit()
    threading.Thread(target=execute_dependency_cmd, args=(dep.id, 'install')).start()
    return jsonify({"status": "success", "id": dep.id})


@app.route('/api/deps/uninstall/<int:id>', methods=['POST'])
@login_required
def api_uninstall_deps(id):
    dep = Dependency.query.get(id)
    if not dep: return jsonify({"status": "error"})
    dep.status = 'Uninstalling'
    db.session.commit()
    threading.Thread(target=execute_dependency_cmd, args=(dep.id, 'uninstall')).start()
    return jsonify({"status": "success"})


@app.route('/api/deps/log/<int:id>')
@login_required
def api_get_deps_log(id):
    dep = Dependency.query.get(id)
    if not dep: return jsonify({"content": "不存在"})
    log_file_path = os.path.join(LOGS_DIR, 'dependencies', f"dep_{dep.id}_{dep.name}.log")
    if os.path.exists(log_file_path):
        with open(log_file_path, 'r', encoding='utf-8') as f: return jsonify({"content": f.read()})
    return jsonify({"content": "暂无日志"})


@app.route('/envs', methods=['GET', 'POST'])
@login_required
def envs():
    if request.method == 'POST':
        env_name = request.form.get('name', '').strip()

        # 【新增：拦截重复名称报错】
        if Env.query.filter_by(name=env_name).first():
            flash(f"添加失败：环境变量 '{env_name}' 已存在，名称必须唯一！")
            return redirect(url_for('envs'))

        now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        # 新建时自动放在末尾
        max_pos = db.session.query(db.func.max(Env.position)).scalar() or 0
        db.session.add(
            Env(name=env_name,
                value=request.form.get('value'),
                remarks=request.form.get('remarks'),
                updated_at=now_str,
                position=max_pos + 1)
        )
        db.session.commit()
        return redirect(url_for('envs'))

    # 获取时根据 position 升序排序
    all_envs = Env.query.order_by(Env.position.asc(), Env.id.asc()).all()
    return render_template('envs.html', envs=all_envs)


@app.route('/api/env/reorder', methods=['POST'])
@login_required
def reorder_envs():
    data = request.json
    if data and 'order' in data:
        order_list = data['order']
        for index, env_id in enumerate(order_list):
            env = Env.query.get(int(env_id))
            if env:
                env.position = index
        db.session.commit()
    return jsonify({"status": "success"})


@app.route('/env/edit/<int:id>', methods=['POST'])
@login_required
def edit_env(id):
    env = Env.query.get(id)
    if env:
        new_name = request.form.get('name', '').strip()

        # 【新增：修改时拦截重名（排除自己本身）】
        existing_env = Env.query.filter_by(name=new_name).first()
        if existing_env and existing_env.id != id:
            flash(f"修改失败：环境变量 '{new_name}' 已被其他项目使用！")
            return redirect(url_for('envs'))

        env.name = new_name
        env.value = request.form.get('value')
        env.remarks = request.form.get('remarks')
        env.updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        db.session.commit()
    return redirect(url_for('envs'))


@app.route('/api/env/toggle/<int:id>')
@login_required
def toggle_env(id):
    env = Env.query.get(id)
    if env:
        env.is_disabled = 1 if getattr(env, 'is_disabled', 0) == 0 else 0
        env.updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
        db.session.commit()
    return redirect(url_for('envs'))


@app.route('/env/delete/<int:id>')
@login_required
def delete_env(id):
    env = Env.query.get(id)
    if env: db.session.delete(env); db.session.commit()
    return redirect(url_for('envs'))


@app.route('/config', methods=['GET', 'POST'])
@login_required
def config_editor():
    if request.method == 'POST':
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write(request.form.get('config_content', '').replace('\r\n', '\n'))
        flash('配置文件已更新')
        return redirect(url_for('config_editor'))
    config_content = ""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: config_content = f.read()
    return render_template('config.html', config_content=config_content)


@app.route('/scripts', methods=['GET', 'POST'])
@login_required
def scripts_editor():
    files = [f for f in os.listdir(SCRIPTS_DIR) if os.path.isfile(os.path.join(SCRIPTS_DIR, f))]

    # 【安全修复 2：防止 GET 目录穿越读取系统文件】
    raw_current_file = request.args.get('file', files[0] if files else 'new_script.py')
    current_file = secure_filename(raw_current_file)

    content = ""
    if request.method == 'POST':
        raw_filename = request.form.get('filename')
        if raw_filename:
            # 【安全修复 3：防止 POST 目录穿越覆写系统文件】
            filename_to_save = secure_filename(raw_filename)
            with open(os.path.join(SCRIPTS_DIR, filename_to_save), 'w', encoding='utf-8') as f:
                f.write(request.form.get('content').replace('\r\n', '\n'))
            return redirect(url_for('scripts_editor', file=filename_to_save))

    if os.path.exists(os.path.join(SCRIPTS_DIR, current_file)):
        with open(os.path.join(SCRIPTS_DIR, current_file), 'r', encoding='utf-8') as f: content = f.read()
    return render_template('scripts.html', files=files, current_file=current_file, content=content)


@app.route('/scripts/debug', methods=['GET', 'POST'])
@login_required
def scripts_debug():
    files = [f for f in os.listdir(SCRIPTS_DIR) if os.path.isfile(os.path.join(SCRIPTS_DIR, f))]

    # 【安全修复 4：GET 目录穿越防御】
    raw_current_file = request.args.get('file', files[0] if files else 'new_script.py')
    current_file = secure_filename(raw_current_file)

    if request.method == 'POST':
        content = request.form.get('content', '').replace('\r\n', '\n')
        with open(os.path.join(SCRIPTS_DIR, current_file), 'w', encoding='utf-8') as f:
            f.write(content)
        flash('脚本保存成功，可进行调试')
        return redirect(url_for('scripts_debug', file=current_file))

    content = ""
    if os.path.exists(os.path.join(SCRIPTS_DIR, current_file)):
        with open(os.path.join(SCRIPTS_DIR, current_file), 'r', encoding='utf-8') as f:
            content = f.read()
    return render_template('debug.html', files=files, current_file=current_file, content=content)


def execute_debug(filename, stream_id):
    with app.app_context():
        try:
            run_env = get_combined_env()
            cmd_list = ['node', '--require', './ql_env.js', filename] if filename.endswith('.js') else \
                ['python', filename] if filename.endswith('.py') else \
                    ['bash', filename] if filename.endswith('.sh') else [filename]

            socketio.emit('log_stream',
                          {'task_id': stream_id, 'data': f"🚀 开始调试执行: {' '.join(cmd_list)}\n", 'clear': True})

            process = subprocess.Popen(cmd_list, shell=False, env=run_env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, cwd=SCRIPTS_DIR, text=True, bufsize=1,
                                       encoding='utf-8', errors='replace')
            for line in process.stdout:
                socketio.emit('log_stream', {'task_id': stream_id, 'data': line})
            process.wait()
            socketio.emit('log_stream',
                          {'task_id': stream_id, 'data': f"\n✅ 调试执行结束，退出码: {process.returncode}\n"})
        except Exception as e:
            socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n❌ 调试出错: {str(e)}\n"})


@app.route('/api/scripts/debug_run', methods=['POST'])
@login_required
def api_debug_run():
    raw_filename = request.form.get('filename')
    if not raw_filename: return jsonify({"status": "error"})

    # 【安全修复 5：防止黑客发送非法文件名执行无关脚本】
    filename = secure_filename(raw_filename)

    stream_id = f"debug_{int(time.time())}"
    threading.Thread(target=execute_debug, args=(filename, stream_id), daemon=True).start()
    return jsonify({"status": "success", "stream_id": stream_id})


@app.route('/api/scripts/check', methods=['POST'])
@login_required
def check_script_syntax():
    raw_filename = request.form.get('filename', '')
    content = request.form.get('content', '')
    if not raw_filename or not content.strip():
        return jsonify({"status": "ok", "msg": ""})

    # 【安全修复 6：同样清理文件名】
    filename = secure_filename(raw_filename)

    try:
        if filename.endswith('.py'):
            ast.parse(content)
            return jsonify({"status": "ok", "msg": "Python 语法无误"})
        elif filename.endswith('.js'):
            with tempfile.NamedTemporaryFile(suffix='.js', delete=False, mode='w', encoding='utf-8') as f:
                f.write(content)
                temp_name = f.name
            try:
                result = subprocess.run(['node', '--check', temp_name], capture_output=True, text=True)
                if result.returncode == 0:
                    return jsonify({"status": "ok", "msg": "Node.js 语法无误"})
                else:
                    err_lines = result.stderr.split('\n')
                    line_num = 1
                    match = re.search(rf"{re.escape(temp_name)}:(\d+)", result.stderr)
                    if match: line_num = int(match.group(1))
                    err = err_lines[0] if err_lines else "JS 语法错误"
                    return jsonify(
                        {"status": "error", "msg": f"第 {line_num} 行错误: {err.replace(temp_name, filename)}",
                         "line": line_num})
            finally:
                os.remove(temp_name)
        elif filename.endswith('.sh'):
            with tempfile.NamedTemporaryFile(suffix='.sh', delete=False, mode='w', encoding='utf-8') as f:
                f.write(content)
                temp_name = f.name
            try:
                result = subprocess.run(['bash', '-n', temp_name], capture_output=True, text=True)
                if result.returncode == 0:
                    return jsonify({"status": "ok", "msg": "Shell 语法无误"})
                else:
                    err = result.stderr.strip()
                    match = re.search(r'line (\d+):', err)
                    line_num = int(match.group(1)) if match else 1
                    return jsonify({"status": "error", "msg": err.replace(temp_name, filename), "line": line_num})
            finally:
                os.remove(temp_name)
        else:
            return jsonify({"status": "ok", "msg": "该文件类型暂无语法校验功能"})
    except SyntaxError as e:
        return jsonify({"status": "error", "msg": f"第 {e.lineno} 行错误: {e.msg}", "line": e.lineno})
    except Exception as e:
        return jsonify({"status": "error", "msg": f"检查异常: {str(e)}", "line": 1})


@app.route('/logs')
@login_required
def logs():
    tree = {}
    for item in sorted(os.listdir(LOGS_DIR)):
        item_path = os.path.join(LOGS_DIR, item)
        if os.path.isdir(item_path): tree[item] = sorted(os.listdir(item_path), reverse=True)

    raw_current_folder = request.args.get('folder')
    raw_current_file = request.args.get('file')
    content = "请在左侧选择需要查看的日志..."

    if raw_current_folder and raw_current_file:
        # 【安全修复 7：防止通过日志路由读取系统核心日志文件】
        current_folder = secure_filename(raw_current_folder)
        current_file = secure_filename(raw_current_file)

        filepath = os.path.join(LOGS_DIR, current_folder, current_file)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f: content = f.read()

    return render_template('logs.html', tree=tree, current_folder=raw_current_folder, current_file=raw_current_file,
                           content=content)


@app.route('/settings', methods=['GET'])
@login_required
def settings():
    configs = {c.key: c.value for c in SystemConfig.query.all()}
    logs = LoginLog.query.order_by(LoginLog.id.desc()).limit(50).all()
    return render_template('settings.html', config=configs, logs=logs)


@app.route('/settings/security', methods=['POST'])
@login_required
def settings_security():
    username = request.form.get('username')
    password = request.form.get('password')
    if username: current_user.username = username
    if password: current_user.password_hash = generate_password_hash(password)
    db.session.commit()
    flash('安全设置已更新')
    return redirect(url_for('settings') + '?tab=security')


@app.route('/settings/config', methods=['POST'])
@login_required
def settings_config():
    tab = request.form.get('tab', 'security')
    for key, value in request.form.items():
        if key == 'tab': continue
        cfg = SystemConfig.query.filter_by(key=key).first()
        if cfg:
            cfg.value = value
        else:
            db.session.add(SystemConfig(key=key, value=value))

        if key == 'timezone':
            os.environ['TZ'] = value
            try:
                time.tzset()
            except AttributeError:
                pass

    db.session.commit()
    flash('配置设置已保存')
    return redirect(url_for('settings') + f'?tab={tab}')


@app.route('/api/settings/backup')
@login_required
def backup_data():
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(DATA_DIR):
            for file in files:
                filepath = os.path.join(root, file)
                arcname = os.path.relpath(filepath, DATA_DIR)
                zf.write(filepath, arcname)
    memory_file.seek(0)
    return send_file(memory_file, download_name=f'pdx_backup_{datetime.now().strftime("%Y%m%d%H%M%S")}.zip',
                     as_attachment=True)


@app.route('/api/avatar')
def get_avatar():
    avatar_path = os.path.join(CONFIG_DIR, 'avatar.png')
    if os.path.exists(avatar_path): return send_file(avatar_path)
    return "Not Found", 404


@app.route('/api/avatar/upload', methods=['POST'])
@login_required
def upload_avatar():
    if 'file' not in request.files:
        flash('未选择文件')
        return redirect(url_for('settings') + '?tab=security')
    file = request.files['file']
    if file.filename == '':
        flash('未选择文件')
        return redirect(url_for('settings') + '?tab=security')
    if file:
        file.save(os.path.join(CONFIG_DIR, 'avatar.png'))
        flash('头像已成功更新')
    return redirect(url_for('settings') + '?tab=security')


def auto_clean_logs():
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
                            os.remove(filepath)
        except:
            pass


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
        except Exception as e:
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

    # 【修复：添加对环境变量排序 position 字段的自动检测与数据库迁移】
    try:
        with env_engine.connect() as conn:
            conn.execute(text('SELECT position FROM env LIMIT 1'))
    except Exception:
        try:
            with env_engine.begin() as conn:
                conn.execute(text('ALTER TABLE env ADD COLUMN position INTEGER DEFAULT 0'))
                # 默认将旧数据的排序值设为与 ID 相同
                conn.execute(text('UPDATE env SET position = id'))
            print("Successfully migrated envs.db with position column.")
        except Exception:
            pass

    try:
        Task.query.filter_by(status='Running').update({'status': 'Idle'})
        Dependency.query.filter(Dependency.status.in_(['Installing', 'Uninstalling'])).update({'status': 'Error'})
        db.session.commit()
    except:
        pass

    ql_env_path = os.path.join(SCRIPTS_DIR, 'ql_env.js')
    if not os.path.exists(ql_env_path):
        with open(ql_env_path, 'w', encoding='utf-8') as f:
            f.write('if (!console.logErr) { console.logErr = function(e) { console.error(e.message || e); }; }\n')

    sys_notify_path = os.path.join(SCRIPTS_DIR, 'sys_notify.js')
    if not os.path.exists(sys_notify_path):
        with open(sys_notify_path, 'w', encoding='utf-8') as f:
            f.write("""try {
    const { sendNotify } = require('./sendNotify.js');
    const title = process.argv[2];
    const content = process.argv[3];
    sendNotify(title, content, {}, '').then(() => process.exit(0)).catch(() => process.exit(1));
} catch(e) {
    console.error('无法调用 sendNotify.js (如需使用通知，请在面板脚本管理中上传该文件):', e.message);
    process.exit(1);
}""")

    # ==========【新增：自动复制初始化 js 脚本】==========
    init_scripts_dir = os.path.join(BASE_DIR, 'init_scripts')
    if os.path.exists(init_scripts_dir):
        for file_name in os.listdir(init_scripts_dir):
            if file_name.endswith('.js'):
                src_file = os.path.join(init_scripts_dir, file_name)
                dst_file = os.path.join(SCRIPTS_DIR, file_name)
                # 如果目标目录(data/scripts)不存在该文件，则复制过去（防止覆盖用户之后在面板中做的修改）
                if not os.path.exists(dst_file):
                    try:
                        shutil.copy2(src_file, dst_file)
                        print(f"Initialized script: {file_name}")
                    except Exception as e:
                        print(f"Failed to copy {file_name}: {str(e)}")
    # ====================================================

# =====================================================================
# 新增：原生终端 CLI 快捷命令拦截 (支持 unblock, untfa, resetpwd)
# =====================================================================
if len(sys.argv) > 1:
    cmd = sys.argv[1]
    if cmd in ['unblock', 'untfa', 'resetpwd']:
        with app.app_context():
            print("\n=======================================")
            print("         派大星面板 - 控制台           ")
            print("=======================================\n")

            if cmd == 'unblock':
                sec = LoginSecurity.query.first()
                if sec:
                    sec.failed_count = 0
                    sec.locked_until = None
                    db.session.commit()
                    print("✅ 成功：登录错误次数已重置，账号已解除锁定！\n")
                else:
                    print("✅ 提示：当前没有被锁定的记录。\n")

            elif cmd == 'untfa':
                totp_cfg = SystemConfig.query.filter_by(key='totp_secret').first()
                if totp_cfg:
                    db.session.delete(totp_cfg)
                    db.session.commit()
                    print("✅ 成功：两步验证(2FA)已被强制禁用！\n")
                else:
                    print("✅ 提示：当前并未开启两步验证。\n")

            elif cmd == 'resetpwd':
                user = User.query.first()
                if not user:
                    print("❌ 错误：系统中尚未创建任何用户，请先访问网页进行初始化配置！\n")
                else:
                    print(f"[*] 当前系统用户名为: {user.username}")
                    # 交互式输入，直接回车代表不修改
                    new_user = input("👉 请输入新用户名 (直接回车则不修改): ").strip()
                    new_pwd = getpass.getpass("👉 请输入新密码 (直接回车则不修改，输入时不显示): ").strip()

                    changed = False
                    if new_user:
                        user.username = new_user
                        changed = True
                    if new_pwd:
                        user.password_hash = generate_password_hash(new_pwd)
                        changed = True

                    if changed:
                        db.session.commit()
                        print("\n✅ 成功：账号/密码已重置！请使用新凭证重新登录页面。\n")
                    else:
                        print("\n[*] 提示：您没有输入新内容，未做任何修改。\n")
        sys.exit(0)
# =====================================================================

if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not os.environ.get('FLASK_DEBUG'):
    with app.app_context():
        try:
            for task in Task.query.all(): add_job_to_scheduler(task)
        except:
            pass

    if not scheduler.running:
        if not scheduler.get_job('sys_log_clean'):
            scheduler.add_job(auto_clean_logs, CronTrigger.from_crontab('0 2 * * *'), id='sys_log_clean')

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
                time.tzset()
        except:
            pass

        scheduler.start()