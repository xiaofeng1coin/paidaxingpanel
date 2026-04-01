import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from flask import request, redirect, url_for, flash, jsonify, session
from flask_login import login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from backend.models import db, User, LoginSecurity, SystemConfig, LoginLog
from backend.core.template import render_template
from backend.core.security import generate_totp_secret, verify_totp
from backend.core.notify import send_sys_notify

def init_app(app):
    @app.route('/install', methods=['GET', 'POST'])
    def install():
        if User.query.count() > 0:
            return redirect(url_for('login'))

        if request.method == 'POST':
            data = request.json
            username = data.get('username', '').strip()
            password = data.get('password', '')

            if not username or not password:
                return jsonify({"status": "error", "msg": "账号和密码不能为空"})

            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)

            notify_type = data.get('notify_type', 'none')
            db.session.add(SystemConfig(key='notify_type', value=notify_type))

            if notify_type == 'telegram':
                db.session.add(SystemConfig(key='TG_BOT_TOKEN', value=data.get('TG_BOT_TOKEN', '')))
                db.session.add(SystemConfig(key='TG_USER_ID', value=data.get('TG_USER_ID', '')))
            elif notify_type == 'dingtalk':
                db.session.add(SystemConfig(key='DD_BOT_TOKEN', value=data.get('DD_BOT_TOKEN', '')))
                db.session.add(SystemConfig(key='DD_BOT_SECRET', value=data.get('DD_BOT_SECRET', '')))
            elif notify_type == 'pushplus':
                db.session.add(SystemConfig(key='PUSH_PLUS_TOKEN', value=data.get('PUSH_PLUS_TOKEN', '')))
            elif notify_type == 'serverchan':
                db.session.add(SystemConfig(key='PUSH_KEY', value=data.get('PUSH_KEY', '')))
            elif notify_type == 'wxpusher':
                db.session.add(SystemConfig(key='WXPUSHER_APP_TOKEN', value=data.get('WXPUSHER_APP_TOKEN', '')))
                db.session.add(SystemConfig(key='WXPUSHER_UID', value=data.get('WXPUSHER_UID', '')))

            db.session.add(LoginSecurity(failed_count=0))
            db.session.commit()
            return jsonify({"status": "success"})

        return render_template('install.html')


    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('tasks'))

        sec = LoginSecurity.query.first()
        if not sec:
            sec = LoginSecurity(failed_count=0)
            db.session.add(sec)
            db.session.commit()

        if request.method == 'POST':
            ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if ip and ',' in ip: ip = ip.split(',')[0].strip()

            ua_string = request.user_agent.string.lower() if request.user_agent else ""
            if any(kw in ua_string for kw in ['mobile', 'android', 'iphone', 'ipad', 'ipod', 'windows phone']):
                device = "mobile"
            else:
                device = "desktop"

            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            def get_address(ip_addr):
                if not ip_addr or ip_addr.startswith("192.") or ip_addr.startswith("10.") or ip_addr.startswith(
                        "172.") or ip_addr == "127.0.0.1" or ip_addr == "::1":
                    return "内网IP"
                try:
                    req = urllib.request.Request(f"http://ip-api.com/json/{ip_addr}?lang=zh-CN",
                                                 headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=3) as res:
                        data = json.loads(res.read().decode('utf-8'))
                        if data.get(
                                'status') == 'success': return f"{data.get('country', '')} {data.get('regionName', '')} {data.get('city', '')}".strip()
                except:
                    pass
                return "未知网络"

            address = get_address(ip)

            req_username = request.form.get('username')
            req_password = request.form.get('password')

            if sec.locked_until and datetime.now() < sec.locked_until:
                flash(f"尝试次数过多，请在 {sec.locked_until.strftime('%H:%M:%S')} 后重试")
                log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="被锁定")
                db.session.add(log);
                db.session.commit()
                send_sys_notify(app, "派大星面板-安全告警",
                            f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：账户因多次失败被锁定")
                return render_template('login.html', temp_user=req_username, temp_pwd=req_password)

            user = User.query.filter_by(username=req_username).first()
            if user and check_password_hash(user.password_hash, req_password):

                totp_secret = SystemConfig.query.filter_by(key='totp_secret').first()
                if totp_secret and totp_secret.value:
                    totp_code = request.form.get('totp_code')
                    if not totp_code:
                        return render_template('login.html', need_2fa=True, temp_user=req_username, temp_pwd=req_password)
                    else:
                        if not verify_totp(totp_secret.value, totp_code):
                            sec.failed_count += 1
                            if sec.failed_count >= 5:
                                lock_mins = (sec.failed_count - 4) * 5
                                sec.locked_until = datetime.now() + timedelta(minutes=lock_mins)
                                flash(f"动态验证码多次错误，账号被锁定 {lock_mins} 分钟。")
                            else:
                                flash(f"两步验证码错误！还可以尝试 {5 - sec.failed_count} 次。")
                            db.session.commit()

                            if sec.locked_until and datetime.now() < sec.locked_until:
                                return redirect(url_for('login'))
                            return render_template('login.html', need_2fa=True, temp_user=req_username,
                                               temp_pwd=req_password)

                sec.failed_count = 0
                sec.locked_until = None

                last_log = LoginLog.query.order_by(LoginLog.id.desc()).first()
                if last_log:
                    session['last_login_info'] = {
                        'time': last_log.login_time,
                        'ip': last_log.ip,
                        'address': last_log.address,
                        'status': last_log.status
                    }

                log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="成功")
                db.session.add(log);
                db.session.commit()

                session.permanent = True
                login_user(user)

                send_sys_notify(app, "派大星面板-登录成功", f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：登录成功")
                return redirect(url_for('tasks'))

            sec.failed_count += 1
            if sec.failed_count >= 5:
                lock_mins = (sec.failed_count - 4) * 5
                sec.locked_until = datetime.now() + timedelta(minutes=lock_mins)
                flash(f"连续登录失败 {sec.failed_count} 次，账号被锁定 {lock_mins} 分钟。")
            else:
                flash(f"账号或密码错误！还有 {5 - sec.failed_count} 次尝试机会。")

            log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="失败")
            db.session.add(log);
            db.session.commit()
            send_sys_notify(app, "派大星面板-登录失败",
                        f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：密码错误（已失败 {sec.failed_count} 次）")

        return render_template('login.html')


    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        return redirect(url_for('login'))


    @app.route('/api/2fa/generate')
    @login_required
    def api_2fa_generate():
        secret = generate_totp_secret()
        issuer = "PatrickPanel"
        account = current_user.username
        label = urllib.parse.quote(f"{issuer}:{account}")
        encoded_issuer = urllib.parse.quote(issuer)
        uri = f"otpauth://totp/{label}?secret={secret}&issuer={encoded_issuer}"
        return jsonify({"secret": secret, "uri": uri})


    @app.route('/api/2fa/enable', methods=['POST'])
    @login_required
    def api_2fa_enable():
        secret = request.form.get('secret')
        code = request.form.get('code')
        if verify_totp(secret, code):
            cfg = SystemConfig.query.filter_by(key='totp_secret').first()
            if cfg:
                cfg.value = secret
            else:
                db.session.add(SystemConfig(key='totp_secret', value=secret))
            db.session.commit()
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "msg": "验证码不正确或已过期"})


    @app.route('/api/2fa/disable', methods=['POST'])
    @login_required
    def api_2fa_disable():
        pwd = request.form.get('password')
        if not check_password_hash(current_user.password_hash, pwd):
            return jsonify({"status": "error", "msg": "系统密码验证失败"})

        cfg = SystemConfig.query.filter_by(key='totp_secret').first()
        if cfg:
            db.session.delete(cfg)
            db.session.commit()
        return jsonify({"status": "success"})