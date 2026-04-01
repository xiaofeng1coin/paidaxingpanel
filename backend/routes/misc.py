import os
from flask import request, redirect, url_for, flash, send_file
from flask_login import login_required
from backend.core.paths import CONFIG_DIR

def init_app(app):
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