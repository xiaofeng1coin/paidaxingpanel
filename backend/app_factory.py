import os
import logging
from datetime import timedelta
from flask import Flask, request, redirect, url_for, flash, session
from backend.models import db, User, SystemConfig
from backend.extensions import socketio, login_manager
from backend.core.paths import DB_DIR, VERSION_FILE, APP_SECRET_KEY, INSTANCE_COOKIE_SUFFIX
from backend.runtime import SUBPROCESS_KWARGS

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
aps_logger = logging.getLogger('apscheduler')
aps_logger.setLevel(logging.DEBUG)

if os.name == 'nt':
    SUBPROCESS_KWARGS['creationflags'] = 0x08000000

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates'),
    static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
)

app.secret_key = APP_SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(DB_DIR, 'auth.db')}"
app.config['SQLALCHEMY_BINDS'] = {
    'tasks': f"sqlite:///{os.path.join(DB_DIR, 'tasks.db')}",
    'envs': f"sqlite:///{os.path.join(DB_DIR, 'envs.db')}"
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 30,
    'max_overflow': 60,
    'pool_timeout': 30,
    'pool_recycle': 1800,
    'pool_pre_ping': True,
    'connect_args': {
        'check_same_thread': False,
        'timeout': 30
    }
}
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = f"patrickpanel_session_{INSTANCE_COOKIE_SUFFIX}"
app.config['SESSION_COOKIE_PATH'] = '/'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

db.init_app(app)

socketio.init_app(app)

login_manager.init_app(app)
login_manager.login_view = 'login'


@socketio.on('connect')
def handle_ws_connect():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return False


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.context_processor
def inject_global_settings():
    sys_theme = 'auto'
    sys_lang = 'auto'
    last_login = None
    sys_version = '1.0.0'
    try:
        from flask_login import current_user
        if current_user.is_authenticated or request.endpoint == 'login':
            configs = {c.key: c.value for c in SystemConfig.query.all()}
            sys_theme = configs.get('theme', 'auto')
            sys_lang = configs.get('language', 'auto')
            if current_user.is_authenticated and 'last_login_info' in session:
                last_login = session.pop('last_login_info', None)

        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE, 'r', encoding='utf-8') as f:
                import json
                sys_version = json.load(f).get('version', '1.0.0')
    except:
        pass
    return dict(sys_theme=sys_theme, sys_lang=sys_lang, last_login_info=last_login, sys_version=sys_version)


@app.before_request
def global_security_check():
    if not request.path.startswith('/static/'):
        if User.query.count() == 0 and request.endpoint not in ['install']:
            return redirect(url_for('install'))

    session.modified = True

    from flask_login import current_user
    allowed_direct_endpoints = [
        'login', 'install', 'logout', 'tasks', 'static', 'subs',
        'scripts_editor', 'scripts_debug', 'api_debug_run', 'api_debug_stop', 'logs', 'deps', 'envs', 'config_editor', 'settings',
        'api_delete_logs', 'api_task_batch', 'api_scripts_save', 'api_scripts_upload', 'api_update_check', 'api_update_do',
        'api_subs_add', 'api_subs_edit', 'api_subs_delete', 'api_subs_run', 'api_subs_log', 'api_subs_delete_tasks', 'api_subs_toggle',
        'get_avatar', 'api_restore_backup', 'api_task_views', 'api_task_views_toggle'
    ]
    if current_user.is_authenticated and request.method == 'GET' and request.endpoint not in allowed_direct_endpoints:
        referer = request.headers.get("Referer")
        if not referer or request.host not in referer:
            flash("为保证安全，禁止直接输入地址访问未授权子页面，已退回首页。")
            return redirect(url_for('tasks'))


@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    csp_policy = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "worker-src 'self' blob:;"
    )
    response.headers['Content-Security-Policy'] = csp_policy
    return response

from backend.services.scheduler_service import add_job_to_scheduler, add_sub_job_to_scheduler, start_scheduler_once
from backend.routes.install_login import init_app as init_install_login_routes
from backend.routes.tasks import init_app as init_task_routes
from backend.routes.subs import init_app as init_sub_routes
from backend.routes.deps import init_app as init_dep_routes
from backend.routes.envs import init_app as init_env_routes
from backend.routes.config_editor import init_app as init_config_editor_routes
from backend.routes.scripts import init_app as init_script_routes
from backend.routes.logs import init_app as init_log_routes
from backend.routes.settings import init_app as init_setting_routes
from backend.routes.update import init_app as init_update_routes
from backend.routes.misc import init_app as init_misc_routes

init_install_login_routes(app)
init_task_routes(app, lambda task: add_job_to_scheduler(app, task))
init_sub_routes(app, lambda sub: add_sub_job_to_scheduler(app, sub))
init_dep_routes(app)
init_env_routes(app)
init_config_editor_routes(app)
init_script_routes(app)
init_log_routes(app)
init_setting_routes(app)
init_update_routes(app)
init_misc_routes(app)

start_scheduler_once(app)