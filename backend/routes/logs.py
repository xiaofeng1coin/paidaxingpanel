import os
from flask import request
from flask_login import login_required
from backend.core.template import render_template
from backend.core.paths import LOGS_DIR
from backend.core.security import get_safe_path

def init_app(app):
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
            # [安全修复] 使用 get_safe_path
            filepath = get_safe_path(LOGS_DIR, os.path.join(raw_current_folder, raw_current_file))
            if filepath and os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f: content = f.read()

        return render_template('logs.html', tree=tree, current_folder=raw_current_folder, current_file=raw_current_file,
                           content=content)