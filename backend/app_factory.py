import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from datetime import timedelta
from urllib.parse import urlparse
from flask import Flask, request, redirect, url_for, flash, session, jsonify
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from backend.models import db, User, SystemConfig
from backend.extensions import socketio, login_manager
from backend.core.paths import DB_DIR, VERSION_FILE, APP_SECRET_KEY, INSTANCE_COOKIE_SUFFIX, DATA_DIR
from backend.core.config_cache import get_all_configs
from backend.core.csrf import get_csrf_token, before_request_csrf
from backend.runtime import SUBPROCESS_KWARGS

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
aps_logger = logging.getLogger('apscheduler')
aps_logger.setLevel(logging.DEBUG)

# [修复 P2-26] 统一文件日志：源码模式原先只有控制台输出，EXE 模式的日志初始化又散在 app.py 里；
# 现在所有模式统一在应用工厂初始化轮转日志（5MB x 3）。
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    _panel_log_path = os.path.join(DATA_DIR, 'panel.log')
    _panel_handler = RotatingFileHandler(_panel_log_path, maxBytes=5 * 1024 * 1024,
                                         backupCount=3, encoding='utf-8')
    _panel_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'))
    logging.getLogger().addHandler(_panel_handler)
except Exception:
    logging.getLogger().warning("轮转日志初始化失败（数据目录不可写？），仅保留控制台日志")

if os.name == 'nt':
    SUBPROCESS_KWARGS['creationflags'] = 0x08000000
else:
    # [修复 P1-4 配套] POSIX 下以独立进程组启动子进程，配合 kill_process_tree 精确终止整棵进程树
    SUBPROCESS_KWARGS['start_new_session'] = True

# [修复 P2-12] SQLite 开启 WAL 与 NORMAL 同步模式，
# 缓解调度器/队列/请求/执行线程多线程并发写导致的 database is locked
@sa_event.listens_for(Engine, 'connect')
def _set_sqlite_pragma(dbapi_conn, conn_record):
    if type(dbapi_conn).__module__ == 'sqlite3' or type(dbapi_conn).__name__ == 'SQLite3CursorWrapper':
        try:
            cur = dbapi_conn.cursor()
            cur.execute('PRAGMA journal_mode=WAL')
            cur.execute('PRAGMA synchronous=NORMAL')
            cur.execute('PRAGMA busy_timeout=30000')
            cur.close()
        except Exception:
            pass

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
# [加固 P3-19] 上传总量上限 1GB → 256MB（覆盖最大场景：备份还原；脚本上传另有 10MB 校验）
app.config['MAX_CONTENT_LENGTH'] = 256 * 1024 * 1024

db.init_app(app)

# 建表必须同步在主线程完成：原先 db.create_all() 放在调度器后台线程里，
# 面板刚启动时访问会撞上"表未建"的 500 竞态。
with app.app_context():
    db.create_all()

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
    # [修复 P2-24] 使用 SQLAlchemy 2.0 推荐的 session.get 替代 legacy Query.get
    return db.session.get(User, int(user_id))


@app.context_processor
def inject_global_settings():
    sys_theme = 'auto'
    sys_lang = 'auto'
    last_login = None
    sys_version = '1.0.0'
    try:
        from flask_login import current_user
        if current_user.is_authenticated or request.endpoint == 'login':
            # [修复 P2-11] SystemConfig 全表查询改为带 TTL 的内存缓存
            configs = get_all_configs(app)
            sys_theme = configs.get('theme', 'auto') or 'auto'
            sys_lang = configs.get('language', 'auto') or 'auto'
            if current_user.is_authenticated and 'last_login_info' in session:
                last_login = session.pop('last_login_info', None)

        if os.path.exists(VERSION_FILE):
            with open(VERSION_FILE, 'r', encoding='utf-8') as f:
                import json
                sys_version = json.load(f).get('version', '1.0.0')
    except Exception:
        pass
    return dict(
        sys_theme=sys_theme, sys_lang=sys_lang, last_login_info=last_login,
        sys_version=sys_version, csrf_token=get_csrf_token()
    )


@app.before_request
def global_security_check():
    # [加固 P2-7] Host 头校验（DNS Rebinding 防护）：
    # EXE 模式仅监听 127.0.0.1，合法 Host 只能是本机回环地址；
    # 恶意网页把自有域名解析到 127.0.0.1 后，浏览器将带着恶意域名的 Host 访问面板——直接拒绝。
    # 源码/手机模式监听 0.0.0.0，访问来源地址不可预知，不做此校验。
    if getattr(sys, 'frozen', False) and not request.path.startswith('/static/'):
        try:
            hostname = urlparse(f"http://{request.host}").hostname
        except Exception:
            hostname = None
        if hostname not in ('127.0.0.1', 'localhost', '::1'):
            return "拒绝访问：Host 头校验失败", 403

    # 未初始化时强制进入安装流程
    if not request.path.startswith('/static/'):
        if User.query.count() == 0 and request.endpoint not in ['install']:
            return redirect(url_for('install'))

    # [加固 P2-3] 凭据版本校验：改密后所有携带旧版本号的会话（含被盗的其他会话）立即失效
    from flask_login import current_user as _cu
    if _cu.is_authenticated and request.path.startswith('/static/') is False:
        if session.get('cred_v') != (_cu.cred_version or 0):
            from flask_login import logout_user
            logout_user()
            session.clear()
            if request.path.startswith('/api/'):
                return jsonify({"status": "expired"}), 401
            return redirect(url_for('login'))

    # [修复 P1-9] CSRF 防护：已登录用户的全部写请求必须携带匹配的 token
    csrf_result = before_request_csrf()
    if csrf_result is not None:
        return csrf_result

    # [修复 P2-15] 拆除原"禁止直连地址"白名单机制：
    # 该机制把几十个 API 端点全量放行、只拦 GET，几乎无防护价值且逻辑复杂，
    # 站点安全职责已由 CSRF 校验(P1-9) + login_required + SameSite Cookie 承担。


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
from backend.routes.api_app import init_app as init_api_app_routes
init_api_app_routes(app)
start_scheduler_once(app)
