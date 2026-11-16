import os
import threading
from datetime import datetime
from flask import request, redirect, url_for, jsonify, flash
from flask_login import login_required
from sqlalchemy import case
from backend.core.cron_utils import build_cron_trigger
from backend.models import db, Task, TaskView
from backend.core.template import render_template
from backend.core.proc_utils import kill_process_tree
from backend.core.file_utils import read_text_tail
from backend.core.task_helpers import sanitize_task_name, compute_next_run
from backend.extensions import scheduler, socketio
from backend.runtime import running_processes, queued_tasks, task_queue
from backend.services.executors import execute_task, enqueue_task_execution, emit_queue_status

def build_tasks_redirect():
    page = request.values.get('page', 1, type=int)
    per_page = request.values.get('per_page', 10, type=int)
    status = request.values.get('status', 'all')
    source = request.values.get('source', 'all')
    keyword = request.values.get('keyword', '').strip()
    return redirect(url_for('tasks', page=page, per_page=per_page, status=status, source=source, keyword=keyword))

def init_app(app, add_job_to_scheduler):
    @app.route('/')
    @login_required
    def tasks():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        status_filter = request.args.get('status', 'all')
        source_filter = request.args.get('source', 'all')
        keyword = request.args.get('keyword', '').strip()

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

        if keyword:
            keyword_like = f"%{keyword}%"
            query = query.filter(
                (Task.name.like(keyword_like)) |
                (Task.command.like(keyword_like))
            )

        pagination = query.order_by(
            case(
                (Task.status == 'Running', 0),
                (Task.status == 'Queued', 1),
                else_=2
            ),
            Task.is_disabled.asc(),
            Task.id.desc()
        ).paginate(page=page, per_page=per_page, error_out=False)

        source_views = TaskView.query.filter_by(is_visible=1).order_by(TaskView.sort_order.asc(), TaskView.id.asc()).all()
        all_views = TaskView.query.order_by(TaskView.sort_order.asc(), TaskView.id.asc()).all()

        tz_str = os.environ.get('TZ', 'Asia/Shanghai')

        for task in pagination.items:
            if getattr(task, 'is_disabled', 0) == 1:
                task.next_run = "已禁用"
            else:
                # [修复 P2-11] next_run 带缓存计算，避免任务多时每行现算 CronTrigger
                task.next_run = compute_next_run(task.id, task.cron, tz_str, build_cron_trigger)

        return render_template('tasks.html', pagination=pagination, status_filter=status_filter, source_filter=source_filter, per_page=per_page, source_views=source_views, all_views=all_views, queued_count=len(task_queue), keyword=keyword)

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
        view = db.session.get(TaskView, id)
        if not view:
            return jsonify({"status": "error", "msg": "视图不存在"})

        view.is_visible = 0 if view.is_visible == 1 else 1
        db.session.commit()
        return jsonify({"status": "success", "is_visible": view.is_visible})

    @app.route('/task/add', methods=['POST'])
    @login_required
    def add_task():
        # [修复 B-1] 配套 APP 客户端分支：带 X-Requested-With 标识头时返回结构化 JSON，
        # 避免"校验失败也 302 被 APP 误判为成功"的问题
        is_app = request.headers.get('X-Requested-With') == 'FlutterPanelApp'

        def _app_error(msg):
            return jsonify({"status": "error", "msg": msg})

        # [修复 P1-7] 任务名净化（防日志目录写穿）+ 命令非空校验
        command = (request.form.get('command') or '').strip()
        if not command:
            if is_app: return _app_error('执行命令不能为空')
            flash('执行命令不能为空')
            return build_tasks_redirect()
        new_task = Task(name=sanitize_task_name(request.form.get('name'), '未命名任务'), command=command,
                    cron=request.form.get('cron'), status='Idle',
                    source_type='manual', source_key='manual', source_name='单脚本')
        db.session.add(new_task);
        db.session.commit();

        from backend.core.task_helpers import ensure_task_view
        ensure_task_view('单脚本', 'manual', 'manual', is_system=1, sort_order=0)

        add_job_to_scheduler(new_task)
        if is_app: return jsonify({"status": "success"})
        return build_tasks_redirect()


    @app.route('/task/edit/<int:id>', methods=['POST'])
    @login_required
    def edit_task(id):
        is_app = request.headers.get('X-Requested-With') == 'FlutterPanelApp'
        task = db.session.get(Task, id)
        if task:
            command = (request.form.get('command') or '').strip()
            if not command:
                if is_app: return jsonify({"status": "error", "msg": "执行命令不能为空"})
                flash('执行命令不能为空')
                return build_tasks_redirect()
            task.name = sanitize_task_name(request.form.get('name'), task.name)
            task.command = command
            task.cron = request.form.get('cron')
            db.session.commit()
            add_job_to_scheduler(task)
        if is_app: return jsonify({"status": "success"})
        return build_tasks_redirect()


    @app.route('/api/task/toggle/<int:id>', methods=['POST'])
    @login_required
    def toggle_task(id):
        task = db.session.get(Task, id)
        if task:
            task.is_disabled = 1 if getattr(task, 'is_disabled', 0) == 0 else 0
            db.session.commit()
            if task.is_disabled == 1:
                if scheduler.get_job(f"task_{task.id}"): scheduler.remove_job(f"task_{task.id}")
            else:
                add_job_to_scheduler(task)
        return build_tasks_redirect()


    @app.route('/task/delete/<int:id>', methods=['POST'])
    @login_required
    def delete_task(id):
        task = db.session.get(Task, id)
        if task:
            if id in running_processes:
                # [修复 P1-4] 终止整棵进程树，避免子进程残留
                kill_process_tree(running_processes.pop(id, None))
            queued_tasks.pop(id, None)
            if id in task_queue:
                try:
                    task_queue.remove(id)
                except ValueError:
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
            task = db.session.get(Task, task_id)
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
                    # [修复 P1-4] 终止整棵进程树
                    kill_process_tree(running_processes.pop(task_id, None))
                queued_tasks.pop(task_id, None)
                if task_id in task_queue:
                    try:
                        task_queue.remove(task_id)
                    except ValueError:
                        pass
                emit_queue_status()
                if scheduler.get_job(f"task_{task.id}"):
                    scheduler.remove_job(f"task_{task.id}")
                db.session.delete(task)

        db.session.commit()
        return jsonify({"status": "success"})


    @app.route('/api/task/run/<int:id>', methods=['POST'])
    @login_required
    def api_run_task(id):
        task = db.session.get(Task, id)
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


    @app.route('/api/task/stop/<int:id>', methods=['POST'])
    @login_required
    def api_stop_task(id):
        task = db.session.get(Task, id)
        if not task: return jsonify({"status": "error"})
        queued_tasks.pop(id, None)
        if id in task_queue:
            try:
                task_queue.remove(id)
            except ValueError:
                pass
        emit_queue_status()
        if id in running_processes:
            # [修复 P1-4] 终止整棵进程树
            kill_process_tree(running_processes.pop(id, None))
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
        task = db.session.get(Task, id)
        if not task: return jsonify({"content": "不存在"})

        task_log_dir = get_safe_path(LOGS_DIR, sanitize_task_name(task.name))
        if task_log_dir and os.path.exists(task_log_dir):
            # [修复 P2-5] 只取 .log 文件并按修改时间取最新，避免目录混入其它文件时取错
            log_files = [f for f in os.listdir(task_log_dir) if f.endswith('.log')]
            if log_files:
                latest = max(log_files, key=lambda f: os.path.getmtime(os.path.join(task_log_dir, f)))
                # [修复 P2-5] 日志过大时只返回末尾内容，避免接口与页面被拖垮
                return jsonify({"content": read_text_tail(os.path.join(task_log_dir, latest))})
        return jsonify({"content": "暂无日志..."})