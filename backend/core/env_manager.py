import os
import re
from backend.models import Env, SystemConfig
from backend.core.paths import NODE_DIR, PYTHON_DIR, CONFIG_FILE

def get_combined_env(app):
    run_env = os.environ.copy()

    # [修复 P2-16] 只按"变量名"过滤 GITHUB 相关变量。
    # 旧实现连"变量值"里含 GITHUB 字样的变量也一并删除，导致值为 github.com 的正常变量被误删；
    # 脚本层对 process.env 的防盗链拦截已由 ql_env.js 的 JSON.stringify 兜底，无需在此删除值。
    keys_to_remove = [k for k in list(run_env.keys()) if 'GITHUB' in k.upper()]
    for k in keys_to_remove:
        run_env.pop(k, None)
        
    run_env['PYTHONUNBUFFERED'] = '1'

    run_env['NODE_PATH'] = os.path.join(NODE_DIR, 'node_modules')
    existing_pythonpath = run_env.get('PYTHONPATH', '')
    run_env['PYTHONPATH'] = f"{PYTHON_DIR}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else PYTHON_DIR

    with app.app_context():
        for e in Env.query.filter((Env.is_disabled == 0) | (Env.is_disabled == None)).order_by(
                Env.position.asc()).all():
            clean_value = str(e.value).replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff')
            run_env[e.name] = clean_value

        proxy_cfg = SystemConfig.query.filter_by(key='proxy').first()
        if proxy_cfg and proxy_cfg.value:
            run_env['HTTP_PROXY'] = proxy_cfg.value
            run_env['HTTPS_PROXY'] = proxy_cfg.value
            run_env['ALL_PROXY'] = proxy_cfg.value

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.replace('\ufeff', '').strip()
                if line.startswith('export '):
                    match = re.match(r'^export\s+([A-Za-z0-9_]+)=[\'"]?(.*?)[\'"]?$', line)
                    if match:
                        run_env[match.group(1)] = match.group(2).replace('\r\n', '\n').replace('\r', '\n').lstrip('\ufeff')
    return run_env