# api_app.py 完整代码
import os
import json
import re
import threading
from datetime import datetime
from flask import jsonify, request
from flask_login import login_required
from apscheduler.triggers.cron import CronTrigger
from backend.models import db, Task, Env, SystemConfig, Dependency
from backend.runtime import running_processes, queued_tasks, task_queue
from backend.core.paths import SCRIPTS_DIR, CONFIG_FILE, LOGS_DIR
from backend.core.security import get_safe_path
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
                try:
                    trigger = CronTrigger.from_crontab(t.cron, timezone=tz_str)
                    now = datetime.now(trigger.timezone)
                    next_time = trigger.get_next_fire_time(None, now)
                    next_run = next_time.strftime("%Y-%m-%d %H:%M:%S") if next_time else "无法计算"
                except: next_run = "规则错误"
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
        env = Env.query.get(request.json.get('id'))
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
            if '.git' in root or '__pycache__' in root: continue
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
        except: return jsonify({"status": "error"})

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
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: f.write(request.json.get('content', '').replace('\r\n', '\n'))
        return jsonify({"status": "success"})

    @app.route('/api/app/security/save', methods=['POST'])
    @login_required
    def app_security_save():
        from flask_login import current_user
        from werkzeug.security import generate_password_hash
        current_user.username = request.json.get('username', '').strip()
        current_user.password_hash = generate_password_hash(request.json.get('password', '').strip())
        db.session.commit()
        return jsonify({"status": "success"})

    # ======== ======== 系统配置、依赖管理与树级日志 ========

    @app.route('/api/app/sysconfig', methods=['GET'])
    @login_required
    def app_sysconfig():
        configs = {c.key: c.value for c in SystemConfig.query.all()}
        return jsonify({"status": "success", "data": configs})

    @app.route('/api/app/sysconfig/save', methods=['POST'])
    @login_required
    def app_sysconfig_save():
        for k, v in request.json.items():
            cfg = SystemConfig.query.filter_by(key=k).first()
            if cfg: cfg.value = str(v)
            else: db.session.add(SystemConfig(key=k, value=str(v)))
        db.session.commit()
        return jsonify({"status": "success"})

    @app.route('/api/app/deps', methods=['GET'])
    @login_required
    def app_deps():
        deps = Dependency.query.order_by(Dependency.id.desc()).all()
        return jsonify({"status": "success", "data": [{"id": d.id, "name": d.name, "pkg_type": d.pkg_type, "status": d.status} for d in deps]})

    @app.route('/api/app/dep/add', methods=['POST'])
    @login_required
    def app_dep_add():
        pkg_type = request.json.get('type')
        package = request.json.get('package', '').strip()
        if not package or not re.match(r'^[A-Za-z0-9_\-\.\@\/=]+$', package): return jsonify({"status": "error", "msg": "非法字符"})
        if find_installed_dependency(package, pkg_type): return jsonify({"status": "error", "msg": "依赖已被安装"})
        dep = Dependency(name=package, pkg_type=pkg_type, status='Installing')
        db.session.add(dep)
        db.session.commit()
        threading.Thread(target=execute_dependency_cmd, args=(app, dep.id, 'install')).start()
        return jsonify({"status": "success"})

    @app.route('/api/app/dep/del', methods=['POST'])
    @login_required
    def app_dep_del():
        dep = Dependency.query.get(request.json.get('id'))
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
            with open(filepath, 'r', encoding='utf-8') as f: return jsonify({"status": "success", "content": f.read()})
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
        
        sub = Subscription.query.get(request.json.get('id'))
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
                        except: pass
            else:
                tasks = Task.query.filter(Task.command.like(f"{sub.alias}/%")).all()
                target_dir = get_safe_path(SCRIPTS_DIR, sub.alias)
                if target_dir and os.path.exists(target_dir) and os.path.isdir(target_dir):
                    try: shutil.rmtree(target_dir, ignore_errors=True)
                    except: pass
            
            for task in tasks:
                if task.id in running_processes:
                    try: running_processes[task.id].kill()
                    except: pass
                if scheduler.get_job(f"task_{task.id}"): scheduler.remove_job(f"task_{task.id}")
                db.session.delete(task)
            db.session.commit()
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "msg": "未知操作行为"})