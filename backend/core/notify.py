import os
import subprocess
import threading
from backend.models import SystemConfig
from backend.core.env_manager import get_combined_env
from backend.core.paths import SCRIPTS_DIR
from backend.runtime import SUBPROCESS_KWARGS

def send_sys_notify(app, title, content):
    def _send():
        with app.app_context():
            configs = {c.key: str(c.value) for c in SystemConfig.query.all() if c.value is not None}
            notify_type = configs.get('notify_type', 'none')
            if notify_type == 'none':
                return

            env = get_combined_env(app)
            for k, v in configs.items(): env[k] = v

            if notify_type == 'telegram':
                env['TG_BOT_TOKEN'] = configs.get('TG_BOT_TOKEN', '')
                env['TG_USER_ID'] = configs.get('TG_USER_ID', '')
            elif notify_type == 'dingtalk':
                env['DD_BOT_TOKEN'] = configs.get('DD_BOT_TOKEN', '')
                env['DD_BOT_SECRET'] = configs.get('DD_BOT_SECRET', '')
            elif notify_type == 'pushplus':
                env['PUSH_PLUS_TOKEN'] = configs.get('PUSH_PLUS_TOKEN', '')
            elif notify_type == 'serverchan':
                env['PUSH_KEY'] = configs.get('PUSH_KEY', '')
            elif notify_type == 'wxpusher':
                wx_token = configs.get('WXPUSHER_APP_TOKEN', '')
                wx_uid = configs.get('WXPUSHER_UID', '')
                env['WXPUSHER_APP_TOKEN'] = wx_token
                env['WXPUSHER_UID'] = wx_uid
                env['WP_APP_TOKEN'] = wx_token
                env['WP_UIDS'] = wx_uid
                env['WP_APP_TOKEN_ONE'] = wx_token
                env['WP_UIDS_ONE'] = wx_uid

            sys_notify_js = os.path.join(SCRIPTS_DIR, 'sys_notify.js')
            if os.path.exists(sys_notify_js):
                try:
                    subprocess.run(['node', 'sys_notify.js', title, content], env=env, cwd=SCRIPTS_DIR, timeout=30, **SUBPROCESS_KWARGS)
                except Exception as e:
                    pass

    threading.Thread(target=_send, daemon=True).start()