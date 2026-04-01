import os
import re
from backend.models import Env, SystemConfig
from backend.core.paths import NODE_DIR, PYTHON_DIR, CONFIG_FILE

def get_combined_env(app):
    run_env = os.environ.copy()
    
    keys_to_remove = [k for k in list(run_env.keys()) if 'GITHUB' in k.upper() or (isinstance(run_env[k], str) and 'GITHUB' in run_env[k].upper())]
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