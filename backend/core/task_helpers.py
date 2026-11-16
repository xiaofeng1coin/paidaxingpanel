import re
import os
import time

from backend.models import db, TaskView


def parse_script_meta(filepath, default_name):
    name = default_name
    cron = None
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(8192)

            name_match = re.search(r'任务名称[：:]\s*([^\r\n*]+)', content, re.IGNORECASE)
            if name_match:
                name = name_match.group(1).strip()

            if name == default_name:
                name_match = re.search(r'\b(?:const|let|var)\s+jsname\s*=\s*[\'"](.+?)[\'"]', content)
                if name_match:
                    name = name_match.group(1).strip()

            if name == default_name:
                name_match = re.search(r'\b(?:new\s+)?Env\s*\(\s*[\'"](.+?)[\'"]\s*\)', content)
                if name_match:
                    name = name_match.group(1).strip()
            
            cron_match = re.search(r'(?:cron|@cron)[^\w\d]+([0-9\*/,-]+\s+[0-9\*/,-]+\s+[0-9\*/,-]+\s+[0-9\*/,-]+\s+[0-9\*/,-]+(?:\s+[0-9\*/,-]+)?)', content, re.IGNORECASE)
            if cron_match:
                raw_cron = cron_match.group(1).strip()
                raw_cron = re.sub(r'\s*\*\s*/\s*$', '', raw_cron)
                cron = raw_cron.strip()
    except Exception:
        pass
    return name, cron


# ==================== [修复 P2-22] TaskView 创建逻辑统一 ====================
# 原先在 executors.py / scheduler_service.py / tasks.py 中重复出现 4 次，统一收敛到这里。
def ensure_task_view(name, source_key, source_type='manual', is_system=0, sort_order=0):
    """确保指定 source_key 的任务视图存在；存在时不修改任何字段。返回是否新建。"""
    if not source_key:
        return False
    if TaskView.query.filter_by(source_key=source_key).first():
        return False
    if sort_order <= 0:
        max_order = db.session.query(db.func.max(TaskView.sort_order)).scalar() or 0
        sort_order = max_order + 1
    db.session.add(TaskView(
        name=name,
        source_key=source_key,
        source_type=source_type,
        is_visible=0,
        is_system=is_system,
        sort_order=sort_order
    ))
    db.session.commit()
    return True


# ==================== [修复 P2-11] next_run 计算缓存 ====================
# 任务列表每行现算 CronTrigger.get_next_fire_time 在任务多时明显变慢；
# 缓存 key 包含 cron 表达式本身，cron 修改后 key 自然变化，无需显式失效。
_next_run_cache = {}


def compute_next_run(task_id, cron, tz_str, build_trigger):
    """带 60 秒 TTL 的下次执行时间计算。build_trigger 由调用方注入（避免循环导入）。"""
    cache_key = f"{task_id}|{cron}|{tz_str}"
    cached = _next_run_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[1] < 60:
        return cached[0]
    result = "无法计算"
    try:
        trigger = build_trigger(cron, timezone=tz_str)
        fire_time = trigger.get_next_fire_time(None, datetime_now(trigger))
        if fire_time:
            result = fire_time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        result = "规则错误"
    if len(_next_run_cache) > 2000:
        _next_run_cache.clear()
    _next_run_cache[cache_key] = (result, now)
    return result


def datetime_now(trigger):
    from datetime import datetime
    return datetime.now(trigger.timezone)


# ==================== [修复 P1-7] 输入校验规则 ====================
ENV_NAME_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
ALIAS_PATTERN = re.compile(r'^[A-Za-z0-9_\-]{1,50}$')
# 任务名/依赖日志名中的 Windows 非法字符与路径分隔符
_UNSAFE_CHARS = re.compile(r'[\\/:*?\"<>|\r\n]')


def validate_env_name(name):
    """环境变量名必须符合 shell 变量规范，防止破坏子进程环境块。"""
    return bool(name) and bool(ENV_NAME_PATTERN.match(name))


def validate_alias(alias):
    """订阅别名仅允许字母/数字/下划线/中划线，防止 git clone 到目录外。"""
    return bool(alias) and bool(ALIAS_PATTERN.match(alias))


def sanitize_task_name(name, fallback='未命名任务'):
    """任务名去除路径分隔符与非法字符，防止日志目录写穿。"""
    cleaned = _UNSAFE_CHARS.sub('', str(name or '')).strip()
    if not cleaned:
        cleaned = fallback
    return cleaned[:80]


def sanitize_dep_log_name(name):
    """依赖日志文件名：scoped npm 包名(@scope/pkg)含斜杠会导致日志写入失败，替换为双下划线。"""
    return _UNSAFE_CHARS.sub('__', str(name or ''))[:100]


def validate_package_name(pkg, pkg_type):
    """[加固 P3-22] 依赖包名校验：
    - 拒绝空名与以 '-' 开头的名字（防止 npm/pip 参数注入，如 --registry=evil）；
    - pip 包名不允许路径分隔符（防止本地路径安装）。"""
    pkg = str(pkg or '').strip()
    if not pkg or pkg.startswith('-'):
        return False
    if not re.match(r'^[A-Za-z0-9_\-\.\@\/=]+$', pkg):
        return False
    if pkg_type == 'pip' and ('/' in pkg or '\\' in pkg):
        return False
    return True


# [加固 P2-16] 镜像源地址校验：仅允许 http(s) URL，防止配置被篡改后拼进命令注入
_MIRROR_RE = re.compile(r'^https?://[A-Za-z0-9\.\-_:]+[A-Za-z0-9\./\-_]*$')


def sanitize_mirror_url(value):
    """镜像源仅接受合法 http(s) 地址，非法值返回空串（调用方忽略该配置）。"""
    v = str(value or '').strip()
    return v if _MIRROR_RE.match(v) else ''