import os
import shutil
import threading
import time
from flask import request, redirect, url_for, jsonify
from flask_login import login_required
from backend.models import db, Subscription, Task
from backend.core.template import render_template
from backend.extensions import scheduler
from backend.runtime import running_processes
from backend.services.executors import execute_subscription
from backend.core.paths import SCRIPTS_DIR, CUSTOM_OVERRIDE_DIR
from backend.core.security import get_safe_path

def init_app(app, add_sub_job_to_scheduler):
    @app.route('/subs')
    @login_required
    def subs():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        pagination = Subscription.query.order_by(Subscription.is_disabled.asc(), Subscription.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        return render_template('subs.html', pagination=pagination, per_page=per_page)

    @app.route('/api/subs/add', methods=['POST'])
    @login_required
    def api_subs_add():
        data = request.json
        alias = data.get('alias', '').strip()
        if not alias:
            alias = f"sub_{int(time.time())}"
        
        new_sub = Subscription(
            name=data.get('name'),
            type=data.get('type', 'public_repo'),
            url=data.get('url'),
            alias=alias,
            branch=data.get('branch', ''),
            schedule_type=data.get('schedule_type', 'crontab'),
            cron=data.get('cron', data.get('schedule', '')),
            whitelist=data.get('whitelist', ''),
            blacklist=data.get('blacklist', ''),
            depend_file=data.get('depend_file', ''),
            extensions=data.get('extensions', ''),
            auto_add=1 if data.get('auto_add') else 0,
            auto_del=1 if data.get('auto_del') else 0,
            status='Idle',
            is_disabled=0
        )
        db.session.add(new_sub)
        db.session.commit()
        add_sub_job_to_scheduler(new_sub)
        return jsonify({"status": "success"})

    @app.route('/api/subs/edit/<int:id>', methods=['POST'])
    @login_required
    def api_subs_edit(id):
        sub = Subscription.query.get(id)
        if not sub:
            return jsonify({"status": "error", "msg": "订阅不存在"})
        data = request.json
        
        sub.name = data.get('name')
        sub.type = data.get('type', 'public_repo')
        sub.url = data.get('url')
        sub.alias = data.get('alias', '').strip() or sub.alias
        sub.branch = data.get('branch', '')
        sub.cron = data.get('cron', data.get('schedule', ''))
        sub.whitelist = data.get('whitelist', '')
        sub.blacklist = data.get('blacklist', '')
        sub.depend_file = data.get('depend_file', '')
        sub.extensions = data.get('extensions', '')
        sub.auto_add = 1 if data.get('auto_add') else 0
        sub.auto_del = 1 if data.get('auto_del') else 0
        
        db.session.commit()
        add_sub_job_to_scheduler(sub)
        return jsonify({"status": "success"})

    @app.route('/api/subs/toggle/<int:id>')
    @login_required
    def api_subs_toggle(id):
        sub = Subscription.query.get(id)
        if sub:
            sub.is_disabled = 1 if getattr(sub, 'is_disabled', 0) == 0 else 0
            db.session.commit()
            if sub.is_disabled == 1:
                if scheduler.get_job(f"sub_{sub.id}"):
                    scheduler.remove_job(f"sub_{sub.id}")
            else:
                add_sub_job_to_scheduler(sub)
        return redirect(url_for('subs'))

    @app.route('/api/subs/run/<int:id>')
    @login_required
    def api_subs_run(id):
        sub = Subscription.query.get(id)
        if not sub:
            return jsonify({"status": "error", "msg": "订阅不存在"})
        threading.Thread(target=execute_subscription, args=(app, id, True)).start()
        return jsonify({"status": "success", "msg": "同步任务已提交"})

    @app.route('/api/subs/sync/<int:id>', methods=['POST'])
    @login_required
    def api_subs_sync(id):
        sub = Subscription.query.get(id)
        if not sub:
            return jsonify({"status": "error", "msg": "订阅不存在"})
        threading.Thread(target=execute_subscription, args=(app, id, True)).start()
        return jsonify({"status": "success", "msg": "同步任务已提交"})

    @app.route('/api/subs/delete/<int:id>')
    @login_required
    def api_subs_delete(id):
        sub = Subscription.query.get(id)
        if sub:
            if scheduler.get_job(f"sub_{sub.id}"):
                scheduler.remove_job(f"sub_{sub.id}")
            db.session.delete(sub)
            db.session.commit()
        return redirect(url_for('subs'))

    @app.route('/api/subs/delete_tasks/<int:id>')
    @login_required
    def api_subs_delete_tasks(id):
        sub = Subscription.query.get(id)
        if sub:
            if sub.type == 'single_file':
                tasks = Task.query.filter(Task.command.like(f"single_scripts/{sub.alias}.%")).all()
                for task in tasks:
                    file_path = get_safe_path(SCRIPTS_DIR, task.command)
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except:
                            pass
            else:
                tasks = Task.query.filter(Task.command.like(f"{sub.alias}/%")).all()
                target_dir = get_safe_path(SCRIPTS_DIR, sub.alias)
                if target_dir and os.path.exists(target_dir) and os.path.isdir(target_dir):
                    try:
                        shutil.rmtree(target_dir, ignore_errors=True)
                    except:
                        pass

            for task in tasks:
                if task.id in running_processes:
                    try:
                        running_processes[task.id].kill()
                    except:
                        pass
                if scheduler.get_job(f"task_{task.id}"):
                    scheduler.remove_job(f"task_{task.id}")
                db.session.delete(task)
                
            db.session.commit()
        return redirect(url_for('subs'))

    @app.route('/api/subs/log/<int:id>')
    @login_required
    def api_subs_log(id):
        log_file_path = os.path.join(__import__('backend.core.paths', fromlist=['LOGS_DIR']).LOGS_DIR, 'subscriptions', f"sub_{id}.log")
        if os.path.exists(log_file_path):
            with open(log_file_path, 'r', encoding='utf-8') as f:
                return jsonify({"content": f.read()})
        return jsonify({"content": "暂无日志或尚未执行过..."})

    @app.route('/api/subs/override_info/<int:id>')
    @login_required
    def api_subs_override_info(id):
        sub = Subscription.query.get(id)
        if not sub:
            return jsonify({"status": "error", "msg": "订阅不存在"})

        override_dir = get_safe_path(CUSTOM_OVERRIDE_DIR, sub.alias)
        exists = bool(override_dir and os.path.exists(override_dir))

        files = []
        if exists:
            for root, dirs, filenames in os.walk(override_dir):
                dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__']]
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(file_path, override_dir).replace('\\', '/')
                    files.append(rel_path)
            files.sort()

        return jsonify({
            "status": "success",
            "data": {
                "name": sub.name,
                "alias": sub.alias,
                "path": f"data/deps/{sub.alias}/",
                "exists": exists,
                "files": files
            }
        })

    @app.route('/subs/add', methods=['POST'])
    @login_required
    def subs_add_compat():
        form = request.form
        alias = (form.get('alias') or '').strip()
        if not alias:
            url = (form.get('url') or '').strip()
            branch = (form.get('branch') or '').strip()
            repo_name = url.split('/')[-1] if url else ''
            if repo_name.endswith('.git'):
                repo_name = repo_name[:-4]
            if form.get('type', 'repo') == 'repo':
                alias = f"{repo_name}_{branch}" if branch else repo_name
            else:
                alias = repo_name.split('.')[0] if repo_name else f"sub_{int(time.time())}"
            alias = alias or f"sub_{int(time.time())}"

        type_value = form.get('type', 'repo')
        db_type = 'public_repo' if type_value == 'repo' else 'single_file'

        new_sub = Subscription(
            name=form.get('name'),
            type=db_type,
            url=form.get('url'),
            alias=alias,
            branch=form.get('branch', ''),
            schedule_type='crontab',
            cron=form.get('schedule', form.get('cron', '')),
            whitelist=form.get('whitelist', ''),
            blacklist=form.get('blacklist', ''),
            depend_file=form.get('depend_file', ''),
            extensions=form.get('extensions', ''),
            auto_add=1,
            auto_del=1,
            status='Idle',
            is_disabled=0
        )
        db.session.add(new_sub)
        db.session.commit()
        add_sub_job_to_scheduler(new_sub)
        return redirect(url_for('subs', page=request.form.get('page', 1)))

    @app.route('/subs/edit/<int:id>', methods=['POST'])
    @login_required
    def subs_edit_compat(id):
        sub = Subscription.query.get(id)
        if not sub:
            return redirect(url_for('subs'))

        form = request.form
        type_value = form.get('type', 'repo')
        db_type = 'public_repo' if type_value == 'repo' else 'single_file'

        sub.name = form.get('name')
        sub.type = db_type
        sub.url = form.get('url')
        sub.branch = form.get('branch', '')
        sub.cron = form.get('schedule', form.get('cron', ''))
        db.session.commit()
        add_sub_job_to_scheduler(sub)
        return redirect(url_for('subs', page=request.form.get('page', 1)))

    @app.route('/subs/delete/<int:id>')
    @login_required
    def subs_delete_compat(id):
        sub = Subscription.query.get(id)
        if sub:
            if scheduler.get_job(f"sub_{sub.id}"):
                scheduler.remove_job(f"sub_{sub.id}")
            db.session.delete(sub)
            db.session.commit()
        return redirect(url_for('subs', page=request.args.get('page', 1)))