import os
from flask import request, redirect, url_for, flash
from flask_login import login_required
from backend.core.template import render_template
from backend.core.paths import CONFIG_FILE

def init_app(app):
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