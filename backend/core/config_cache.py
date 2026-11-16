# config_cache.py SystemConfig 内存缓存
# [修复 P2-11] inject_global_settings 原先每个请求都全表查询 SystemConfig，
# 现改为带 TTL 的进程内缓存；配置写操作后需调用 invalidate() 主动失效。
import time
import threading

from backend.models import SystemConfig

_TTL_SECONDS = 30

_cache = {'data': None, 'ts': 0.0}
_lock = threading.Lock()

# [加固 P2-2] 系统配置允许写入的键白名单。
# 此前 sysconfig 保存端点接受任意键，攻击者可借被盗会话直接改写 totp_secret 静默关闭两步验证。
# totp_secret 及其相关键一律不在白名单内，2FA 变更只能走带密码验证的专用接口。
ALLOWED_SYS_CONFIG_KEYS = {
    'theme', 'language', 'notify_type',
    'TG_BOT_TOKEN', 'TG_USER_ID', 'DD_BOT_TOKEN', 'DD_BOT_SECRET',
    'PUSH_PLUS_TOKEN', 'PUSH_KEY', 'WXPUSHER_APP_TOKEN', 'WXPUSHER_UID',
    'node_mirror', 'python_mirror', 'linux_mirror', 'proxy',
    'timezone', 'task_concurrency', 'log_clean_days', 'listen_host',
}


def is_allowed_sys_key(key):
    return str(key) in ALLOWED_SYS_CONFIG_KEYS


def get_all_configs(app):
    """返回 {key: value} 字典，30 秒 TTL 缓存；未初始化完成或查询失败时返回空字典。"""
    now = time.time()
    cached = _cache.get('data')
    if cached is not None and now - _cache['ts'] < _TTL_SECONDS:
        return cached
    try:
        with app.app_context():
            data = {c.key: c.value for c in SystemConfig.query.all()}
    except Exception:
        # 查询失败时回退旧缓存（若有）
        return cached if cached is not None else {}
    with _lock:
        _cache['data'] = data
        _cache['ts'] = now
    return data


def invalidate():
    """配置发生写入后调用，立刻失效缓存。"""
    with _lock:
        _cache['data'] = None
        _cache['ts'] = 0.0
