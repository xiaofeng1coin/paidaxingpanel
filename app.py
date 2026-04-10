#!/usr/bin/env python3
from backend.app_factory import app, socketio
import os
import sys
import threading
import logging
import traceback
from werkzeug.security import generate_password_hash
from backend.core.paths import DATA_DIR, BASE_DIR
from backend.models import db, User, LoginSecurity

def ensure_login_security():
    sec = LoginSecurity.query.first()
    if not sec:
        sec = LoginSecurity(failed_count=0)
        db.session.add(sec)
        db.session.commit()
    return sec

def run_cli():
    if len(sys.argv) < 2:
        return False

    cmd = sys.argv[1].strip().lower()

    if cmd not in ['resetlock', 'resetuser']:
        return False

    with app.app_context():
        if cmd == 'resetlock':
            sec = ensure_login_security()
            sec.failed_count = 0
            sec.locked_until = None
            db.session.commit()
            print("✅ 登录锁定状态已重置成功")
            print("   failed_count = 0")
            print("   locked_until = NULL")
            return True

        if cmd == 'resetuser':
            if len(sys.argv) < 4:
                print("❌ 用法错误")
                print("正确用法: pdx resetuser 用户名 密码")
                sys.exit(1)

            new_username = sys.argv[2]
            new_password = sys.argv[3]

            user = User.query.order_by(User.id.asc()).first()
            if not user:
                print("❌ 未找到管理员账号，请先完成初始化安装")
                sys.exit(1)

            user.username = new_username
            user.password_hash = generate_password_hash(new_password)

            sec = ensure_login_security()
            sec.failed_count = 0
            sec.locked_until = None

            db.session.commit()

            print("✅ 用户名密码已重置成功")
            print(f"   username = {new_username}")
            print("   password = [已更新]")
            print("   登录锁定状态已同步清除")
            return True

    return False

if __name__ == '__main__':
    if run_cli():
        sys.exit(0)

    if getattr(sys, 'frozen', False):
        import webview
        from pystray import Icon, Menu, MenuItem
        from PIL import Image, ImageDraw
        import ctypes
        
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "PatrickPanel_SingleInstance_Mutex")
        if ctypes.windll.kernel32.GetLastError() == 183:
            sys.exit(0)

        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)

        pc_log_path = os.path.join(DATA_DIR, 'pc_debug.log')
        pc_file_handler = logging.FileHandler(pc_log_path, encoding='utf-8')
        pc_file_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s'))
        logging.getLogger().addHandler(pc_file_handler)
        logging.getLogger().setLevel(logging.DEBUG)
        
        wv_log = logging.getLogger('pywebview')
        wv_log.setLevel(logging.DEBUG)
        wv_log.addHandler(pc_file_handler)
        logging.debug("系统已启动，日志模块初始化完成")
        
        def start_server():
            logging.debug("正在启动本地 Flask 服务器...")
            socketio.run(app, host='127.0.0.1', port=5000, allow_unsafe_werkzeug=True)

        threading.Thread(target=start_server, daemon=True).start()
        
        # [修复] 移除 icon 参数，Windows 平台 create_window 不支持该参数
        window = webview.create_window('派大星面板', 'http://127.0.0.1:5000', width=1280, height=800, text_select=True)

        def on_closing():
            logging.debug("触发 on_closing 事件: 用户点击了右上角关闭按钮")
            def _async_hide():
                try:
                    logging.debug("正在尝试隐藏窗口...")
                    window.hide()
                    logging.debug("窗口隐藏成功")
                except Exception as e:
                    logging.error(f"窗口隐藏失败: {traceback.format_exc()}")
            threading.Thread(target=_async_hide, daemon=True).start()
            return False

        window.events.closing += on_closing

        def show_window(icon, item):
            logging.debug("触发托盘菜单: 显示面板")
            window.show()

        def exit_app(icon, item):
            logging.debug("触发托盘菜单: 彻底退出，准备强制杀停进程")
            icon.stop()
            os._exit(0)

        def create_tray_icon():
            try:
                logo_path = os.path.join(BASE_DIR, 'static', 'images', 'logo.png')
                if os.path.exists(logo_path):
                    image = Image.open(logo_path).convert('RGB')
                    if image.size != (64, 64):
                        image = image.resize((64, 64), Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.ANTIALIAS)
                    return image
            except:
                pass
            image = Image.new('RGB', (64, 64), color=(37, 99, 235))
            draw = ImageDraw.Draw(image)
            draw.rectangle((16, 16, 48, 48), fill="white")
            return image

        tray_menu = Menu(
            MenuItem("显示面板", show_window, default=True),
            MenuItem("彻底退出", exit_app)
        )
        tray_icon = Icon("PatrickPanel", create_tray_icon(), "派大星面板", menu=tray_menu)

        threading.Thread(target=tray_icon.run, daemon=True).start()
        
        logging.debug("拉起 webview 主窗口...")
        webview.start()
    else:
        socketio.run(app, host='0.0.0.0', port=5000)