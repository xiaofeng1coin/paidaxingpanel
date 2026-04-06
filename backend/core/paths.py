import os
import sys
import json
import hashlib
import uuid

if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    DATA_DIR = os.path.join(os.path.dirname(sys.executable), 'data')
else:
    BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    if os.environ.get('ANDROID_DATA_DIR'):
        DATA_DIR = os.environ.get('ANDROID_DATA_DIR')
    elif os.environ.get('TERMUX_DATA_DIR'):
        DATA_DIR = os.environ.get('TERMUX_DATA_DIR')
    else:
        DATA_DIR = os.path.join(BASE_DIR, 'data')

SCRIPTS_DIR = os.path.join(DATA_DIR, 'scripts')
LOGS_DIR = os.path.join(DATA_DIR, 'logs')
CONFIG_DIR = os.path.join(DATA_DIR, 'config')
CUSTOM_OVERRIDE_DIR = os.path.join(DATA_DIR, 'deps')

if os.environ.get('TERMUX_DATA_DIR'):
    PRIVATE_DATA_DIR = os.environ.get('TERMUX_PRIVATE_DIR', os.path.join(BASE_DIR, 'data_private'))
    DB_DIR = os.path.join(PRIVATE_DATA_DIR, 'db')
    DEPS_ENV_DIR = os.path.join(PRIVATE_DATA_DIR, 'deps_env')
else:
    DB_DIR = os.path.join(DATA_DIR, 'db')
    DEPS_ENV_DIR = os.path.join(DATA_DIR, 'deps_env')

NODE_DIR = os.path.join(DEPS_ENV_DIR, 'nodejs')
PYTHON_DIR = os.path.join(DEPS_ENV_DIR, 'python')
LINUX_DIR = os.path.join(DEPS_ENV_DIR, 'linux')

CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.sh')
VERSION_FILE = os.path.join(BASE_DIR, 'version.json')

os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(NODE_DIR, exist_ok=True)
os.makedirs(PYTHON_DIR, exist_ok=True)
os.makedirs(LINUX_DIR, exist_ok=True)
os.makedirs(CUSTOM_OVERRIDE_DIR, exist_ok=True)

node_pkg_json = os.path.join(NODE_DIR, 'package.json')
if not os.path.exists(node_pkg_json):
    with open(node_pkg_json, 'w', encoding='utf-8') as f:
        f.write('{"name": "pdx-deps","version": "1.0.0"}')

default_config_lines = [
    "# 派大星面板 Global Config",
    "# Format: export KEY=\"VALUE\"",
    "",
    "# 全局任务最大执行时间(小时)，支持小数如0.1",
    "export TASK_TIMEOUT=\"1\"",
    "",
    "# 自动扫描 scripts 目录时排除的文件名，多个用英文逗号分隔",
    "export AUTO_IMPORT_EXCLUDE_FILES=\"ql_env.js,sys_notify.js,sendNotify.js,ql.js,common.js,utils.js,util.js,david_cookies.js,xfj_sign.js,untils.js\"",
    ""
]

if not os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        f.write("\n".join(default_config_lines))
else:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    changed = False

    if 'TASK_TIMEOUT' not in content:
        content += "\n# 全局任务最大执行时间(小时)，支持小数如0.1\nexport TASK_TIMEOUT=\"1\"\n"
        changed = True
    elif 'export TASK_TIMEOUT="3600"' in content:
        content = content.replace('export TASK_TIMEOUT="3600"', 'export TASK_TIMEOUT="1"')
        changed = True

    if 'AUTO_IMPORT_EXCLUDE_FILES' not in content:
        content += "\n# 自动扫描 scripts 目录时排除的文件名，多个用英文逗号分隔\nexport AUTO_IMPORT_EXCLUDE_FILES=\"ql_env.js,sys_notify.js,sendNotify.js,ql.js,common.js,utils.js,util.js,david_cookies.js,xfj_sign.js,untils.js\"\n"
        changed = True

    if changed:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write(content)

if not os.path.exists(VERSION_FILE):
    with open(VERSION_FILE, 'w', encoding='utf-8') as f:
        json.dump({"version": "1.0.0", "changelog": "初始化版本"}, f, ensure_ascii=False, indent=4)

INSTANCE_ID_FILE = os.path.join(DB_DIR, 'instance_id')
if os.path.exists(INSTANCE_ID_FILE):
    with open(INSTANCE_ID_FILE, 'r', encoding='utf-8') as f:
        _instance_id = f.read().strip()
else:
    _instance_id = uuid.uuid4().hex
    with open(INSTANCE_ID_FILE, 'w', encoding='utf-8') as f:
        f.write(_instance_id)

APP_SECRET_KEY = hashlib.sha256(_instance_id.encode('utf-8')).hexdigest()
INSTANCE_COOKIE_SUFFIX = hashlib.md5(_instance_id.encode('utf-8')).hexdigest()[:12]