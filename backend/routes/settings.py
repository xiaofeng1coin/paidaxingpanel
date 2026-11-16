import os
import io
import sys
import time
import shutil
import tempfile
import subprocess
import zipfile
from datetime import datetime
from flask import request, redirect, url_for, flash, jsonify, send_file, session
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from backend.models import db, SystemConfig, LoginLog, Task, Dependency, Subscription
from backend.core.template import render_template
from backend.core.proc_utils import kill_process_tree
from backend.core.config_cache import invalidate as invalidate_config_cache
from backend.core.paths import DATA_DIR, DB_DIR, LOGS_DIR, SCRIPTS_DIR, CONFIG_DIR, DEPS_ENV_DIR
from backend.extensions import scheduler, socketio
from backend.runtime import running_processes, debug_processes

def init_app(app):
    @app.route('/settings', methods=['GET'])
    @login_required
    def settings():
        configs = {c.key: c.value for c in SystemConfig.query.all()}
        # [加固] totp_secret 不下发明文，模板只需布尔判断"是否已启用"（与 /api/app/sysconfig 一致）
        if 'totp_secret' in configs:
            configs['totp_secret'] = '1' if (configs.get('totp_secret') or '').strip() else ''
        
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
        from backend.core.security import verify_totp
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        current_password = request.form.get('current_password') or ''
        # [修复 P0-3] 与 App 端接口一致：禁止把用户名/密码改空，防止面板锁死
        # [加固 P2-3] 修改凭据必须验证当前密码（有 2FA 时再验动态码）
        if not username:
            flash('用户名不能为空')
            return redirect(url_for('settings') + '?tab=security')
        if password and len(password) < 8:
            flash('密码长度不能少于 8 位')
            return redirect(url_for('settings') + '?tab=security')
        if not current_user.password_hash or not check_password_hash(current_user.password_hash, current_password):
            flash('当前密码验证失败，请输入您正在使用的密码')
            return redirect(url_for('settings') + '?tab=security')
        totp_secret = SystemConfig.query.filter_by(key='totp_secret').first()
        if totp_secret and totp_secret.value:
            totp_ok, _ = verify_totp(totp_secret.value, request.form.get('totp_code') or '')
            if not totp_ok:
                flash('两步验证码错误或未填写')
                return redirect(url_for('settings') + '?tab=security')
        current_user.username = username
        if password:
            current_user.password_hash = generate_password_hash(password)
        # [加固 P2-3] 递增凭据版本：使其他全部旧会话失效，仅保留当前会话
        current_user.cred_version = (current_user.cred_version or 0) + 1
        db.session.commit()
        session['cred_v'] = current_user.cred_version
        invalidate_config_cache()
        flash('安全设置已更新')
        return redirect(url_for('settings') + '?tab=security')


    @app.route('/settings/config', methods=['POST'])
    @login_required
    def settings_config():
        from backend.core.config_cache import is_allowed_sys_key
        tab = request.form.get('tab', 'security')
        # [加固 P2-2] 键白名单：任意键写入已被禁止（含 totp_secret 在内的敏感键拒绝）
        for key, value in request.form.items():
            # 'tab' 为路由参数；'csrf_token' 为表单隐藏域（[回归修复] 不属于配置键，直接跳过）
            if key in ('tab', 'csrf_token'): continue
            if not is_allowed_sys_key(key):
                flash(f'不允许写入配置项: {key}')
                return redirect(url_for('settings') + f'?tab={tab}')
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
        # [修复 P2-11] 配置写入后失效缓存
        invalidate_config_cache()
        flash('配置设置已保存')
        return redirect(url_for('settings') + f'?tab={tab}')


    @app.route('/api/settings/backup')
    @login_required
    def backup_data():
        # [优化] 改为先流式写入临时文件再下发，数据目录很大时不再整包驻留内存
        tmp = tempfile.NamedTemporaryFile(suffix='.zip', delete=False)
        tmp.close()
        try:
            with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(DATA_DIR):
                    dirs[:] = [d for d in dirs if d != '__pycache__']
                    for file in files:
                        filepath = os.path.join(root, file)
                        arcname = os.path.relpath(filepath, DATA_DIR)
                        zf.write(filepath, arcname)
            resp = send_file(tmp.name,
                             download_name=f'pdx_backup_{datetime.now().strftime("%Y%m%d%H%M%S")}.zip',
                             as_attachment=True)
            # 响应流结束后删除临时文件
            resp.call_on_close(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
            return resp
        except Exception:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
            raise


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
                    except Exception:
                        pass
            except Exception:
                pass

            # [修复 P1-5 配套] 还原前终止所有正在运行的任务与调试进程，防止文件/数据库占用
            for proc in list(running_processes.values()):
                kill_process_tree(proc)
            running_processes.clear()
            for proc in list(debug_processes.values()):
                kill_process_tree(proc)
            debug_processes.clear()

            try:
                db.session.remove()
            except Exception:
                pass

            # [加固 P2-12] 归还数据库连接池持有的全部文件句柄：
            # Windows 下不 dispose 会导致 rmtree 删不掉被占用的 db 文件（ignore_errors 静默跳过），
            # 造成旧库残留与新库混装
            try:
                with app.app_context():
                    for engine in db.engines.values():
                        engine.dispose()
            except Exception:
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
                except Exception:
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
                        except Exception:
                            pass
                if os.path.isdir(src_path):
                    shutil.copytree(src_path, dst_path)
                else:
                    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                    shutil.copy2(src_path, dst_path)

            socketio.emit('log_stream', {'task_id': 'sys_restore', 'data': '✅ 数据还原完成，系统即将重启...\n', 'clear': True})

            def _delayed_exit():
                time.sleep(1.5)
                # [修复 P1-5] EXE 环境在退出前先拉起新实例，避免"即将重启"后窗口直接关闭无人拉起
                try:
                    if getattr(sys, 'frozen', False):
                        subprocess.Popen([sys.executable], cwd=os.path.dirname(sys.executable), close_fds=True)
                except Exception:
                    pass
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
                except Exception:
                    pass