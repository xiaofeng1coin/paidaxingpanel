# api_app.py 完整代码
import os
import json
import re
import threading
from datetime import datetime
from flask import jsonify, request, session
from flask_login import login_required
from backend.core.cron_utils import build_cron_trigger
from backend.models import db, Task, Env, SystemConfig, Dependency
from backend.runtime import running_processes, queued_tasks, task_queue
from backend.core.paths import SCRIPTS_DIR, CONFIG_FILE, LOGS_DIR
from backend.core.security import get_safe_path
from backend.core.proc_utils import kill_process_tree
from backend.core.file_utils import read_text_tail
from backend.core.task_helpers import compute_next_run, validate_env_name, sanitize_dep_log_name
from backend.core.config_cache import invalidate as invalidate_config_cache
from backend.core.dependency_manager import find_installed_dependency
from backend.services.executors import execute_dependency_cmd

def init_app(app):
    # ======== 原有：任务与环境变量 ========
    @app.route('/api/app/tasks', methods=['GET'])
    @login_required
    def app_tasks():
        tasks = Task.query.all()
        task_list = []
        tz_str = os.environ.get('TZ', 'Asia/Shanghai')
        for t in tasks:
            next_run = "已禁用"
            if getattr(t, 'is_disabled', 0) != 1:
                # [修复 P2-11] next_run 带缓存计算
                next_run = compute_next_run(t.id, t.cron, tz_str, build_cron_trigger)
            real_status = t.status
            if t.id in running_processes: real_status = 'Running'
            elif t.id in queued_tasks or t.id in task_queue: real_status = 'Queued'
            
            # 👉 核心变更：输出 source_key 等视图信息，让前后端实现视图完全打通
            task_list.append({
                "id": t.id, "name": t.name, "command": t.command, "cron": t.cron, "status": real_status, 
                "last_run": t.last_run, "last_duration": t.last_duration, "is_disabled": getattr(t, 'is_disabled', 0), 
                "next_run": next_run, "source_key": getattr(t, 'source_key', 'manual'), 
                "source_type": getattr(t, 'source_type', 'manual'), "source_name": getattr(t, 'source_name', '单脚本')
            })
        return jsonify({"status": "success", "data": task_list, "queued_count": len(task_queue)})

    @app.route('/api/app/envs', methods=['GET'])
    @login_required
    def app_envs():
        envs = Env.query.order_by(Env.position.asc(), Env.id.asc()).all()
        return jsonify({"status": "success", "data": [{"id": e.id, "name": e.name, "value": e.value, "remarks": e.remarks, "is_disabled": getattr(e, 'is_disabled', 0), "position": e.position} for e in envs]})

    @app.route('/api/app/env/action', methods=['POST'])
    @login_required
    def app_env_action():
        env = db.session.get(Env, request.json.get('id'))
        if not env: return jsonify({"status": "error"})
        action = request.json.get('action')
        if action == 'toggle': env.is_disabled = 1 if getattr(env, 'is_disabled', 0) == 0 else 0
        elif action == 'delete': db.session.delete(env)
        db.session.commit()
        return jsonify({"status": "success"})

    # ======== 原有：脚本文件处理与安全 ========
    @app.route('/api/app/scripts', methods=['GET'])
    @login_required
    def app_scripts():
        all_files = []
        for root, dirs, files in os.walk(SCRIPTS_DIR):
            # [加固 P3-24] 按路径分量判断
            walk_parts = root.replace('\\', '/').split('/')
            if '.git' in walk_parts or '__pycache__' in walk_parts: continue
            rel_dir = os.path.relpath(root, SCRIPTS_DIR).replace('\\', '/')
            if rel_dir == '.': rel_dir = '根目录'
            for f in files:
                if f.endswith(('.js', '.py', '.sh', '.json', '.txt')):
                    file_path = os.path.join(root, f)
                    size = f"{os.path.getsize(file_path) / 1024:.1f} KB"
                    mtime = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime('%Y-%m-%d %H:%M')
                    rel_file = f if rel_dir == '根目录' else f"{rel_dir}/{f}"
                    all_files.append({"name": f, "path": rel_file, "dir": rel_dir, "size": size, "mtime": mtime})
        return jsonify({"status": "success", "data": all_files})

    @app.route('/api/app/script/read', methods=['GET'])
    @login_required
    def app_script_read():
        target_path = get_safe_path(SCRIPTS_DIR, request.args.get('file', ''))
        if not target_path or not os.path.exists(target_path): return jsonify({"status": "error"})
        try:
            with open(target_path, 'r', encoding='utf-8') as f: return jsonify({"status": "success", "content": f.read()})
        except Exception: return jsonify({"status": "error"})

    @app.route('/api/app/config', methods=['GET'])
    @login_required
    def app_config_read():
        content = ""
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: content = f.read()
        return jsonify({"status": "success", "content": content})

    @app.route('/api/app/config/save', methods=['POST'])
    @login_required
    def app_config_save():
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: f.write((request.json.get('content') or '').replace('\r\n', '\n'))
        return jsonify({"status": "success"})

    @app.route('/api/app/security/save', methods=['POST'])
    @login_required
    def app_security_save():
        from flask_login import current_user
        from werkzeug.security import generate_password_hash, check_password_hash
        from backend.core.security import verify_totp
        # [修复 P0-3] 空值会把用户名/密码置空导致面板锁死（空密码可登录），必须校验
        # [加固 P2-3] 修改用户名/密码必须验证当前密码（有 2FA 时再验动态码），
        # 否则会话 Cookie 被盗即可永久接管账号
        username = (request.json.get('username') or '').strip()
        password = (request.json.get('password') or '').strip()
        current_password = request.json.get('current_password') or ''
        if not username:
            return jsonify({"status": "error", "msg": "用户名不能为空"})
        if not password:
            return jsonify({"status": "error", "msg": "密码不能为空"})
        if len(password) < 8:
            return jsonify({"status": "error", "msg": "密码长度不能少于 8 位"})
        if not current_user.password_hash or not check_password_hash(current_user.password_hash, current_password):
            return jsonify({"status": "error", "msg": "当前密码验证失败，请输入您正在使用的密码"})
        totp_secret = SystemConfig.query.filter_by(key='totp_secret').first()
        if totp_secret and totp_secret.value:
            totp_ok, _ = verify_totp(totp_secret.value, request.json.get('totp_code') or '')
            if not totp_ok:
                return jsonify({"status": "error", "msg": "两步验证码错误或未填写"})
        current_user.username = username
        current_user.password_hash = generate_password_hash(password)
        # [加固 P2-3] 递增凭据版本：使其他全部旧会话失效，仅保留当前会话
        current_user.cred_version = (current_user.cred_version or 0) + 1
        db.session.commit()
        session['cred_v'] = current_user.cred_version
        return jsonify({"status": "success"})

    # ======== ======== 系统配置、依赖管理与树级日志 ========

    @app.route('/api/app/sysconfig', methods=['GET'])
    @login_required
    def app_sysconfig():
        configs = {c.key: c.value for c in SystemConfig.query.all()}
        # [加固] totp_secret 不下发明文（被盗会话可读取 2FA 密钥），仅返回是否已设置的布尔语义
        if 'totp_secret' in configs:
            configs['totp_secret'] = '1' if (configs.get('totp_secret') or '').strip() else ''
        return jsonify({"status": "success", "data": configs})

    @app.route('/api/app/sysconfig/save', methods=['POST'])
    @login_required
    def app_sysconfig_save():
        from backend.core.config_cache import is_allowed_sys_key
        # [加固 P2-2] 改为键白名单：不再接受任意键写入（旧实现可借被盗会话
        # 直接改写 totp_secret 静默关闭两步验证）。非法键一律拒绝。
        for k, v in request.json.items():
            if not is_allowed_sys_key(k):
                return jsonify({"status": "error", "msg": f"不允许写入配置项: {k}"}), 403
            cfg = SystemConfig.query.filter_by(key=k).first()
            if cfg: cfg.value = str(v)
            else: db.session.add(SystemConfig(key=k, value=str(v)))
        db.session.commit()
        # [修复 P2-11] 配置写入后失效缓存
        invalidate_config_cache()
        return jsonify({"status": "success"})

    @app.route('/api/app/deps', methods=['GET'])
    @login_required
    def app_deps():
        deps = Dependency.query.order_by(Dependency.id.desc()).all()
        return jsonify({"status": "success", "data": [{"id": d.id, "name": d.name, "pkg_type": d.pkg_type, "status": d.status} for d in deps]})

    @app.route('/api/app/dep/add', methods=['POST'])
    @login_required
    def app_dep_add():
        from backend.core.task_helpers import validate_package_name
        pkg_type = request.json.get('type')
        package = request.json.get('package', '').strip()
        # [加固 P3-22] 包名统一校验（拒绝 '-' 开头的参数注入、pip 路径分隔符等）
        if pkg_type not in ('npm', 'pip', 'linux'): return jsonify({"status": "error", "msg": "不支持的依赖类型"})
        if not validate_package_name(package, pkg_type): return jsonify({"status": "error", "msg": "非法字符"})
        if find_installed_dependency(package, pkg_type): return jsonify({"status": "error", "msg": "依赖已被安装"})
        dep = Dependency(name=package, pkg_type=pkg_type, status='Installing')
        db.session.add(dep)
        db.session.commit()
        threading.Thread(target=execute_dependency_cmd, args=(app, dep.id, 'install')).start()
        return jsonify({"status": "success"})

    @app.route('/api/app/dep/del', methods=['POST'])
    @login_required
    def app_dep_del():
        dep = db.session.get(Dependency, request.json.get('id'))
        if not dep: return jsonify({"status": "error"})
        if dep.status == 'Error':
            db.session.delete(dep)
            db.session.commit()
            return jsonify({"status": "success"})
        dep.status = 'Uninstalling'
        db.session.commit()
        threading.Thread(target=execute_dependency_cmd, args=(app, dep.id, 'uninstall')).start()
        return jsonify({"status": "success"})

    @app.route('/api/app/syslogs', methods=['GET'])
    @login_required
    def app_syslogs():
        tree = []
        if os.path.exists(LOGS_DIR):
            for item in sorted(os.listdir(LOGS_DIR)):
                item_path = os.path.join(LOGS_DIR, item)
                if os.path.isdir(item_path):
                    files = sorted([f for f in os.listdir(item_path) if f.endswith('.log')], reverse=True)
                    if files: tree.append({"folder": item, "files": files})
        return jsonify({"status": "success", "data": tree})

    @app.route('/api/app/syslog/read', methods=['GET'])
    @login_required
    def app_syslog_read():
        filepath = get_safe_path(LOGS_DIR, os.path.join(request.args.get('folder', ''), request.args.get('file', '')))
        if filepath and os.path.exists(filepath):
            # [修复 P2-5] 日志过大时只返回末尾内容
            return jsonify({"status": "success", "content": read_text_tail(filepath)})
        return jsonify({"status": "error"})

    # ======== 👇 核心强力新增：App 订阅管理专属无障碍接口 ========
    @app.route('/api/app/subs', methods=['GET'])
    @login_required
    def app_subs():
        from backend.models import Subscription
        subs = Subscription.query.order_by(Subscription.is_disabled.asc(), Subscription.id.desc()).all()
        return jsonify({"status": "success", "data": [
            {
                "id": s.id, "name": s.name, "type": s.type, "url": s.url, "alias": s.alias,
                "branch": s.branch, "cron": s.cron, "status": s.status, "last_run": s.last_run,
                "is_disabled": getattr(s, 'is_disabled', 0), "whitelist": s.whitelist, "blacklist": s.blacklist,
                "depend_file": s.depend_file, "extensions": s.extensions, "auto_add": s.auto_add, "auto_del": s.auto_del
            } for s in subs
        ]})

    @app.route('/api/app/sub/action', methods=['POST'])
    @login_required
    def app_sub_action():
        from backend.models import Subscription, Task
        from backend.extensions import scheduler
        import os, shutil
        from backend.core.paths import SCRIPTS_DIR
        from backend.core.security import get_safe_path
        from backend.runtime import running_processes

        sub = db.session.get(Subscription, request.json.get('id'))
        if not sub: return jsonify({"status": "error", "msg": "订阅不存在"})
        action = request.json.get('action')

        if action == 'toggle':
            sub.is_disabled = 1 if getattr(sub, 'is_disabled', 0) == 0 else 0
            db.session.commit()
            if sub.is_disabled == 1:
                if scheduler.get_job(f"sub_{sub.id}"): scheduler.remove_job(f"sub_{sub.id}")
            else:
                from backend.services.scheduler_service import add_sub_job_to_scheduler
                add_sub_job_to_scheduler(app, sub)
            return jsonify({"status": "success"})
        elif action == 'delete':
            if scheduler.get_job(f"sub_{sub.id}"): scheduler.remove_job(f"sub_{sub.id}")
            db.session.delete(sub)
            db.session.commit()
            return jsonify({"status": "success"})
        elif action == 'delete_tasks':
            if sub.type == 'single_file':
                tasks = Task.query.filter(Task.command.like(f"single_scripts/{sub.alias}.%")).all()
                for task in tasks:
                    file_path = get_safe_path(SCRIPTS_DIR, task.command)
                    if file_path and os.path.exists(file_path):
                        try: os.remove(file_path)
                        except OSError: pass
            else:
                tasks = Task.query.filter(Task.command.like(f"{sub.alias}/%")).all()
                target_dir = get_safe_path(SCRIPTS_DIR, sub.alias)
                if target_dir and os.path.exists(target_dir) and os.path.isdir(target_dir):
                    try: shutil.rmtree(target_dir, ignore_errors=True)
                    except OSError: pass

            for task in tasks:
                if task.id in running_processes:
                    # [修复 P1-4] 终止整棵进程树
                    kill_process_tree(running_processes.pop(task.id, None))
                if scheduler.get_job(f"task_{task.id}"): scheduler.remove_job(f"task_{task.id}")
                db.session.delete(task)
            db.session.commit()
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "msg": "未知操作行为"})