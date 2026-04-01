import os
import io
import zipfile
import time
import shutil
import tempfile
from datetime import datetime
from flask import request, redirect, url_for, flash, jsonify, send_file
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from backend.models import db, SystemConfig, LoginLog, Task, Dependency, Subscription
from backend.core.template import render_template
from backend.core.paths import DATA_DIR, DB_DIR, LOGS_DIR, SCRIPTS_DIR, CONFIG_DIR, DEPS_ENV_DIR
from backend.extensions import scheduler, socketio

def init_app(app):
    @app.route('/settings', methods=['GET'])
    @login_required
    def settings():
        configs = {c.key: c.value for c in SystemConfig.query.all()}
        
        page = request.args.get('page', 1, type=int)
        status_filter = request.args.get('status', 'all')
        per_page = request.args.get('per_page', 20, type=int)
        
        query = LoginLog.query
        if status_filter != 'all':
            query = query.filter(LoginLog.status == status_filter)
            
        pagination = query.order_by(LoginLog.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        
        return render_template('settings.html', config=configs, pagination=pagination, status_filter=status_filter, per_page=per_page)


    @app.route('/api/logs/delete', methods=['POST'])
    @login_required
    def api_delete_logs():
        data = request.json
        del_type = data.get('type')
        
        if del_type == 'selected':
            ids = data.get('ids', [])
            if ids:
                LoginLog.query.filter(LoginLog.id.in_(ids)).delete(synchronize_session=False)
                db.session.commit()
        elif del_type == 'status':
            status = data.get('status', 'all')
            if status == 'all':
                LoginLog.query.delete()
            else:
                LoginLog.query.filter_by(status=status).delete()
            db.session.commit()
            
        return jsonify({"status": "success"})


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
                dirs[:] = [d for d in dirs if d != '__pycache__']
                for file in files:
                    filepath = os.path.join(root, file)
                    arcname = os.path.relpath(filepath, DATA_DIR)
                    zf.write(filepath, arcname)
        memory_file.seek(0)
        return send_file(memory_file, download_name=f'pdx_backup_{datetime.now().strftime("%Y%m%d%H%M%S")}.zip',
                     as_attachment=True)


    @app.route('/api/settings/restore', methods=['POST'])
    @login_required
    def api_restore_backup():
        if 'file' not in request.files:
            return jsonify({"status": "error", "msg": "未选择备份文件"})

        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"status": "error", "msg": "未选择备份文件"})

        if not file.filename.lower().endswith('.zip'):
            return jsonify({"status": "error", "msg": "仅支持还原 zip 备份文件"})

        temp_dir = None
        backup_zip_path = None

        try:
            temp_dir = tempfile.mkdtemp(prefix='pdx_restore_')
            backup_zip_path = os.path.join(temp_dir, 'restore.zip')
            file.save(backup_zip_path)

            if not zipfile.is_zipfile(backup_zip_path):
                return jsonify({"status": "error", "msg": "上传文件不是有效的 zip 压缩包"})

            extract_dir = os.path.join(temp_dir, 'extract')
            os.makedirs(extract_dir, exist_ok=True)

            with zipfile.ZipFile(backup_zip_path, 'r') as zf:
                members = zf.namelist()
                if not members:
                    return jsonify({"status": "error", "msg": "备份包为空，无法还原"})

                for member in members:
                    normalized = member.replace('\\', '/').strip('/')
                    if not normalized:
                        continue
                    if normalized.startswith('../') or '/../' in f'/{normalized}/':
                        return jsonify({"status": "error", "msg": "备份包内存在非法路径，已拒绝还原"})

                zf.extractall(extract_dir)

            # 关闭调度器任务，避免还原过程中有文件/数据库占用
            try:
                for job in scheduler.get_jobs():
                    try:
                        scheduler.remove_job(job.id)
                    except:
                        pass
            except:
                pass

            try:
                db.session.remove()
            except:
                pass

            restore_targets = [
                DB_DIR,
                LOGS_DIR,
                SCRIPTS_DIR,
                CONFIG_DIR,
                DEPS_ENV_DIR
            ]

            for target in restore_targets:
                try:
                    if os.path.exists(target):
                        shutil.rmtree(target, ignore_errors=True)
                except:
                    pass

            os.makedirs(DATA_DIR, exist_ok=True)

            for item in os.listdir(extract_dir):
                src_path = os.path.join(extract_dir, item)
                dst_path = os.path.join(DATA_DIR, item)
                if os.path.exists(dst_path):
                    if os.path.isdir(dst_path):
                        shutil.rmtree(dst_path, ignore_errors=True)
                    else:
                        try:
                            os.remove(dst_path)
                        except:
                            pass
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path)
                else:
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(src_path, dst_path)

            socketio.emit('log_stream', {'task_id': 'sys_restore', 'data': '✅ 数据还原完成，系统即将重启...\n', 'clear': True})

            def _delayed_exit():
                time.sleep(1.5)
                os._exit(0)

            import threading
            threading.Thread(target=_delayed_exit, daemon=True).start()

            return jsonify({"status": "success", "msg": "数据还原成功，系统即将重启"})

        except Exception as e:
            return jsonify({"status": "error", "msg": f"还原失败: {str(e)}"})
        finally:
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except:
                    pass