import threading
from datetime import datetime
from flask import request, redirect, url_for, jsonify
from flask_login import login_required
from apscheduler.triggers.cron import CronTrigger
from backend.models import db, Task, TaskView
from backend.core.template import render_template
from backend.extensions import scheduler, socketio
from backend.runtime import running_processes, queued_tasks, task_queue
from backend.services.executors import execute_task, enqueue_task_execution, emit_queue_status

def build_tasks_redirect():
    page = request.values.get('page', 1, type=int)
    per_page = request.values.get('per_page', 10, type=int)
    status = request.values.get('status', 'all')
    source = request.values.get('source', 'all')
    return redirect(url_for('tasks', page=page, per_page=per_page, status=status, source=source))

def init_app(app, add_job_to_scheduler):
    @app.route('/')
    @login_required
    def tasks():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        status_filter = request.args.get('status', 'all')
        source_filter = request.args.get('source', 'all')

        query = Task.query

        if source_filter != 'all':
            if source_filter == 'manual':
                query = query.filter(Task.source_type == 'manual')
            else:
                query = query.filter(Task.source_key == source_filter)
            status_filter = 'all'
        else:
            if status_filter == 'normal':
                query = query.filter((Task.is_disabled == 0) | (Task.is_disabled == None))
            elif status_filter == 'disabled':
                query = query.filter(Task.is_disabled == 1)
            source_filter = 'all'

        pagination = query.order_by(Task.is_disabled.asc(), Task.id.desc()).paginate(page=page, per_page=per_page, error_out=False)

        source_views = TaskView.query.filter_by(is_visible=1).order_by(TaskView.sort_order.asc(), TaskView.id.asc()).all()
        all_views = TaskView.query.order_by(TaskView.sort_order.asc(), TaskView.id.asc()).all()

        tz_str = os.environ.get('TZ', 'Asia/Shanghai')

        for task in pagination.items:
            if getattr(task, 'is_disabled', 0) == 1:
                task.next_run = "已禁用"
            else:
                try:
                    trigger = CronTrigger.from_crontab(task.cron, timezone=tz_str)
                    now = datetime.now(trigger.timezone)
                    next_time = trigger.get_next_fire_time(None, now)
                    if next_time:
                        task.next_run = next_time.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        task.next_run = "无法计算"
                except Exception:
                    task.next_run = "规则错误"

        return render_template('tasks.html', pagination=pagination, status_filter=status_filter, source_filter=source_filter, per_page=per_page, source_views=source_views, all_views=all_views, queued_count=len(task_queue))

    @app.route('/api/task_views')
    @login_required
    def api_task_views():
        views = TaskView.query.order_by(TaskView.sort_order.asc(), TaskView.id.asc()).all()
        return jsonify({
            "status": "success",
            "data": [
                {
                    "id": v.id,
                    "name": v.name,
                    "source_key": v.source_key,
                    "source_type": v.source_type,
                    "is_visible": v.is_visible,
                    "is_system": v.is_system
                } for v in views
            ]
        })


    @app.route('/api/task_views/toggle/<int:id>', methods=['POST'])
    @login_required
    def api_task_views_toggle(id):
        view = TaskView.query.get(id)
        if not view:
            return jsonify({"status": "error", "msg": "视图不存在"})

        view.is_visible = 0 if view.is_visible == 1 else 1
        db.session.commit()
        return jsonify({"status": "success", "is_visible": view.is_visible})

    @app.route('/task/add', methods=['POST'])
    @login_required
    def add_task():
        new_task = Task(name=request.form.get('name'), command=request.form.get('command').strip(),
                    cron=request.form.get('cron'), status='Idle',
                    source_type='manual', source_key='manual', source_name='单脚本')
        db.session.add(new_task);
        db.session.commit();

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

        add_job_to_scheduler(new_task)
        return build_tasks_redirect()


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
        return build_tasks_redirect()


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
        return build_tasks_redirect()


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
            queued_tasks.pop(id, None)
            if id in task_queue:
                try:
                    task_queue.remove(id)
                except:
                    pass
            emit_queue_status()
            if scheduler.get_job(f"task_{task.id}"): scheduler.remove_job(f"task_{task.id}")
            db.session.delete(task);
            db.session.commit()
        return build_tasks_redirect()


    @app.route('/api/task/batch', methods=['POST'])
    @login_required
    def api_task_batch():
        data = request.json
        action = data.get('action')
        ids = data.get('ids', [])
        if not ids:
            return jsonify({"status": "error"})
        
        for task_id in ids:
            task = Task.query.get(task_id)
            if not task:
                continue
            
            if action == 'enable':
                task.is_disabled = 0
                add_job_to_scheduler(task)
            elif action == 'disable':
                task.is_disabled = 1
                if scheduler.get_job(f"task_{task.id}"): 
                    scheduler.remove_job(f"task_{task.id}")
            elif action == 'run':
                if task_id not in running_processes and task_id not in queued_tasks:
                    enqueue_task_execution(app, task_id, True, 'manual')
            elif action == 'delete':
                if task_id in running_processes:
                    try:
                        running_processes[task_id].kill()
                    except:
                        pass
                queued_tasks.pop(task_id, None)
                if task_id in task_queue:
                    try:
                        task_queue.remove(task_id)
                    except:
                        pass
                emit_queue_status()
                if scheduler.get_job(f"task_{task.id}"): 
                    scheduler.remove_job(f"task_{task.id}")
                db.session.delete(task)
                
        db.session.commit()
        return jsonify({"status": "success"})


    @app.route('/api/task/run/<int:id>')
    @login_required
    def api_run_task(id):
        task = Task.query.get(id)
        if not task: return jsonify({"status": "error", "msg": "任务不存在"})
        if id in running_processes or task.status == 'Running':
            return jsonify({"status": "error", "msg": "运行中"})
        if id in queued_tasks or task.status == 'Queued':
            return jsonify({"status": "error", "msg": "已在队列中"})

        task.status = 'Queued'
        task.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.session.commit()
        socketio.emit('task_status', {'task_id': task.id, 'status': 'Queued', 'last_run': task.last_run})

        enqueue_task_execution(app, id, True, 'manual')
        return jsonify({"status": "success"})


    @app.route('/api/task/stop/<int:id>')
    @login_required
    def api_stop_task(id):
        task = Task.query.get(id)
        if not task: return jsonify({"status": "error"})
        queued_tasks.pop(id, None)
        if id in task_queue:
            try:
                task_queue.remove(id)
            except:
                pass
        emit_queue_status()
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
        from backend.core.paths import LOGS_DIR
        from backend.core.security import get_safe_path
        task = Task.query.get(id)
        if not task: return jsonify({"content": "不存在"})
        
        task_log_dir = get_safe_path(LOGS_DIR, task.name)
        if task_log_dir and os.path.exists(task_log_dir):
            files = sorted(os.listdir(task_log_dir), reverse=True)
            if files:
                with open(os.path.join(task_log_dir, files[0]), 'r', encoding='utf-8') as f: 
                    return jsonify({"content": f.read()})
        return jsonify({"content": "暂无日志..."})

import os