import os
import json
import time
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from flask import request, redirect, url_for, flash, jsonify, session
from flask_login import login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from backend.models import db, User, LoginSecurity, SystemConfig, LoginLog
from backend.core.template import render_template
from backend.core.security import generate_totp_secret, verify_totp
from backend.core.config_cache import invalidate as invalidate_config_cache
from backend.core.notify import send_sys_notify

# ==================== [修复 P1-10] 登录侧加固 ====================
_geo_cache = {}          # 公网 IP -> 归属地缓存，避免每次登录同步请求第三方
_geo_cache_lock = threading.Lock()

# 按 IP 维度的登录失败限速（与全局 LoginSecurity 锁并存）
_ip_fail_counter = {}    # ip -> {'fails': int, 'locked_until': datetime}
_ip_lock_lock = threading.Lock()
_IP_MAX_FAILS = 10
_IP_LOCK_MINUTES = 15

# [加固 P3-26] 安装接口进程内互斥锁：防并发重复创建管理员
_install_lock = threading.Lock()

# ==================== [修复 P2-19] 首次安装限时窗口 ====================
_INSTALL_WINDOW_HOURS = 48


def _private_addr(addr):
    """[加固 P2-6] 使用标准库 ipaddress 判断私网地址。
    旧实现用 startswith('192.')/('172.')，但 192/8 与 172/8 大部分是公网网段，
    导致公网来源被误判为内网、XFF 被信任，从而绕过按 IP 限速并污染登录日志。"""
    import ipaddress as _ip
    if not addr:
        return False
    try:
        return _ip.ip_address(addr).is_private
    except ValueError:
        return False  # 无法解析的来源一律不视为可信内网（不采信其 XFF）


def _resolve_client_ip():
    """[修复 P1-10] 仅在直连来源为私网地址（存在反向代理的典型场景）时才采信 X-Forwarded-For，
    否则一律使用 remote_addr，防止伪造 XFF 污染登录日志与限速。"""
    remote = request.remote_addr or ''
    xff = request.headers.get('X-Forwarded-For')
    if xff and _private_addr(remote):
        return xff.split(',')[0].strip()
    return remote


def _check_ip_lock(ip):
    with _ip_lock_lock:
        info = _ip_fail_counter.get(ip)
        if info and info.get('locked_until') and datetime.now() < info['locked_until']:
            return True
    return False


def _register_ip_fail(ip):
    with _ip_lock_lock:
        info = _ip_fail_counter.setdefault(ip, {'fails': 0, 'locked_until': None})
        info['fails'] += 1
        if info['fails'] >= _IP_MAX_FAILS:
            info['locked_until'] = datetime.now() + timedelta(minutes=_IP_LOCK_MINUTES)


def _clear_ip_fail(ip):
    with _ip_lock_lock:
        _ip_fail_counter.pop(ip, None)


def init_app(app):
    install_window_file = None
    from backend.core.paths import DB_DIR
    install_window_file = os.path.join(DB_DIR, 'install_window')

    @app.route('/install', methods=['GET', 'POST'])
    def install():
        if User.query.count() > 0:
            return redirect(url_for('login'))

        # [修复 P2-19] 首次安装限时窗口：首次访问 install 页时开始计时 48 小时，
        # 超时未初始化则拒绝安装，防止公网部署期间被抢注管理员。
        try:
            if not os.path.exists(install_window_file):
                with open(install_window_file, 'w', encoding='utf-8') as f:
                    f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            with open(install_window_file, 'r', encoding='utf-8') as f:
                window_start = datetime.strptime(f.read().strip(), "%Y-%m-%d %H:%M:%S")
            install_expired = datetime.now() > window_start + timedelta(hours=_INSTALL_WINDOW_HOURS)
        except Exception:
            # [加固 P3-26] 窗口文件异常时按"已过期"处理（fail-closed），
            # 不再放行安装接口——否则窗口保护在异常场景下形同虚设
            install_expired = True

        if install_expired:
            tip = "安装窗口已超时关闭（超过 48 小时未完成初始化）。请删除 data/db 目录下的 auth.db 与 install_window 文件后重新运行面板以重新初始化。"
            if request.method == 'POST':
                return jsonify({"status": "error", "msg": tip})
            return f"<h3>无法安装</h3><p>{tip}</p>", 403

        if request.method == 'POST':
            # [加固 P3-26] 整个创建流程持进程内互斥锁：并发安装请求只有一个生效
            with _install_lock:
                if User.query.count() > 0:
                    return jsonify({"status": "error", "msg": "管理员已初始化"})

                data = request.json
                username = (data.get('username') or '').strip()
                password = data.get('password') or ''

                if not username or not password:
                    return jsonify({"status": "error", "msg": "账号和密码不能为空"})
                if len(password) < 8:
                    return jsonify({"status": "error", "msg": "密码长度不能少于 8 位"})

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
                invalidate_config_cache()
                return jsonify({"status": "success"})

        return render_template('install.html')


    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('tasks'))

        # [加固 P1-1] 重新进入登录页即放弃两步验证第二步的待验证状态
        if request.method == 'GET':
            session.pop('pending_2fa_user_id', None)

        sec = LoginSecurity.query.first()
        if not sec:
            sec = LoginSecurity(failed_count=0)
            db.session.add(sec)
            db.session.commit()

        if request.method == 'POST':
            # [修复 P1-10] 仅在内网直连（存在反代）时才采信 X-Forwarded-For
            ip = _resolve_client_ip()

            ua_string = request.user_agent.string.lower() if request.user_agent else ""
            if any(kw in ua_string for kw in ['mobile', 'android', 'iphone', 'ipad', 'ipod', 'windows phone']):
                device = "mobile"
            else:
                device = "desktop"

            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            def get_address(ip_addr):
                if not ip_addr or _private_addr(ip_addr):
                    return "内网IP"
                # [修复 P1-10] 归属地查询加缓存，重复 IP 不再每次同步请求第三方拖慢登录
                with _geo_cache_lock:
                    cached = _geo_cache.get(ip_addr)
                if cached:
                    return cached
                try:
                    # [加固 P2-13] 改用 https 免费接口，不再明文上报登录者 IP
                    req = urllib.request.Request(f"https://ipwho.is/{ip_addr}",
                                                 headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=3) as res:
                        data = json.loads(res.read().decode('utf-8'))
                        if data.get('success') is True:
                            result = f"{data.get('country') or ''} {data.get('region') or ''} {data.get('city') or ''}".strip()
                            with _geo_cache_lock:
                                if len(_geo_cache) > 1000:
                                    _geo_cache.clear()
                                _geo_cache[ip_addr] = result
                            return result
                except Exception:
                    pass
                return "未知网络"

            address = get_address(ip)

            req_username = request.form.get('username')
            req_password = request.form.get('password')
            req_totp = request.form.get('totp_code')

            # [修复 B-4/A-1] 配套 APP 客户端分支：APP 带 X-Requested-With 标识头时返回结构化 JSON，
            # 让 APP 能区分 need_2fa / 失败原因 / 成功，而不是解析网页。
            is_app = request.headers.get('X-Requested-With') == 'FlutterPanelApp'

            def _app_error(msg):
                return jsonify({"status": "error", "msg": msg})

            # [加固 P3-17] TOTP 防重放：记录最近成功使用的时间步
            def _get_last_totp_step():
                cfg = SystemConfig.query.filter_by(key='totp_last_used_step').first()
                try:
                    return int(cfg.value) if cfg and cfg.value else None
                except (TypeError, ValueError):
                    return None

            def _save_last_totp_step(step):
                if step is None:
                    return
                cfg = SystemConfig.query.filter_by(key='totp_last_used_step').first()
                if cfg:
                    cfg.value = str(step)
                else:
                    db.session.add(SystemConfig(key='totp_last_used_step', value=str(step)))
                db.session.commit()

            def _login_success(user):
                """统一登录成功流程（第一步直登 / 两步验证通过共用）。"""
                sec.failed_count = 0
                sec.locked_until = None
                _clear_ip_fail(ip)
                session.pop('pending_2fa_user_id', None)

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
                # [加固 P2-3] 记录凭据版本：改密后旧版本号会话全部失效
                session['cred_v'] = user.cred_version or 0
                login_user(user)

                send_sys_notify(app, "派大星面板-登录成功", f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：登录成功")
                if is_app:
                    # 成功时同样下发 Set-Cookie（jsonify 响应自动携带），APP 从响应头提取
                    return jsonify({"status": "success"})
                return redirect(url_for('tasks'))

            def _totp_fail():
                """TOTP 校验失败统一处理：累计失败计数并按配置锁定。"""
                sec.failed_count += 1
                _register_ip_fail(ip)
                if sec.failed_count >= 5:
                    lock_mins = (sec.failed_count - 4) * 5
                    sec.locked_until = datetime.now() + timedelta(minutes=lock_mins)
                    msg = f"动态验证码多次错误，账号被锁定 {lock_mins} 分钟。"
                else:
                    msg = f"两步验证码错误！还可以尝试 {5 - sec.failed_count} 次。"
                db.session.commit()
                log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="两步验证失败")
                db.session.add(log)
                db.session.commit()
                if is_app:
                    return _app_error(msg)
                if sec.locked_until and datetime.now() < sec.locked_until:
                    return redirect(url_for('login'))
                flash(msg)
                return render_template('login.html', need_2fa=True)

            # [修复 P1-10] IP 维度限速：同一来源连续失败 10 次锁定 15 分钟
            if _check_ip_lock(ip):
                msg = f"该来源登录失败次数过多，已临时限制，请 {_IP_LOCK_MINUTES} 分钟后再试"
                log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="IP限制")
                db.session.add(log);
                db.session.commit()
                if is_app: return _app_error(msg)
                flash(msg)
                return render_template('login.html', temp_user=req_username)

            if sec.locked_until and datetime.now() < sec.locked_until:
                msg = f"尝试次数过多，请在 {sec.locked_until.strftime('%H:%M:%S')} 后重试"
                log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="被锁定")
                db.session.add(log);
                db.session.commit()
                send_sys_notify(app, "派大星面板-安全告警",
                            f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：账户因多次失败被锁定")
                if is_app: return _app_error(msg)
                # [修复 P0-4] 不再将用户输入的明文密码回填进 HTML
                flash(msg)
                return render_template('login.html', temp_user=req_username)

            # [加固 P1-1] 两步验证第二步：不再依赖前端回传密码，
            # 仅凭服务端 session 中的待验证用户 id + 动态码完成校验。
            pending_id = session.get('pending_2fa_user_id')
            if pending_id and not req_password:
                user = db.session.get(User, int(pending_id)) if str(pending_id).isdigit() else None
                totp_secret = SystemConfig.query.filter_by(key='totp_secret').first()
                if not user or not totp_secret or not totp_secret.value:
                    session.pop('pending_2fa_user_id', None)
                    msg = '两步验证会话已失效，请重新登录'
                    if is_app: return _app_error(msg)
                    flash(msg)
                    return render_template('login.html')
                ok, used_step = verify_totp(totp_secret.value, req_totp, _get_last_totp_step())
                if ok:
                    _save_last_totp_step(used_step)
                    return _login_success(user)
                return _totp_fail()

            user = User.query.filter_by(username=req_username).first()
            if user and check_password_hash(user.password_hash, req_password or ''):

                totp_secret = SystemConfig.query.filter_by(key='totp_secret').first()
                if totp_secret and totp_secret.value:
                    if not req_totp:
                        # 第一步（仅密码）通过：把待验证用户写入服务端 session，进入第二步
                        if is_app: return jsonify({"status": "need_2fa"})
                        session['pending_2fa_user_id'] = user.id
                        return render_template('login.html', need_2fa=True)
                    # 全字段提交（APP 携码重试 / 表单一次带码）：密码已验，直接校验 TOTP（含防重放）
                    ok, used_step = verify_totp(totp_secret.value, req_totp, _get_last_totp_step())
                    if not ok:
                        return _totp_fail()
                    _save_last_totp_step(used_step)

                return _login_success(user)

            sec.failed_count += 1
            _register_ip_fail(ip)
            if sec.failed_count >= 5:
                lock_mins = (sec.failed_count - 4) * 5
                sec.locked_until = datetime.now() + timedelta(minutes=lock_mins)
                fail_msg = f"连续登录失败 {sec.failed_count} 次，账号被锁定 {lock_mins} 分钟。"
            else:
                fail_msg = f"账号或密码错误！还有 {5 - sec.failed_count} 次尝试机会。"
            if is_app:
                return _app_error(fail_msg)
            flash(fail_msg)

            log = LoginLog(login_time=time_str, address=address, ip=ip, device=device, status="失败")
            db.session.add(log);
            db.session.commit()
            send_sys_notify(app, "派大星面板-登录失败",
                        f"时间：{time_str}\nIP：{ip}\n地点：{address}\n状态：密码错误（已失败 {sec.failed_count} 次）")

        return render_template('login.html')


    @app.route('/logout', methods=['POST'])
    @login_required
    def logout():
        # [加固 P3-18] 登出由 GET 改为 POST，避免被跨站链接/图片强制登出
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
        # [加固] 启用两步验证同样必须验证当前密码：
        # 被盗会话若可直接启用 2FA，会把管理员卡死在登录页（DoS 接管链的一环）
        current_password = request.form.get('current_password') or ''
        if not current_user.password_hash or not check_password_hash(current_user.password_hash, current_password):
            return jsonify({"status": "error", "msg": "当前密码验证失败"})
        secret = request.form.get('secret')
        code = request.form.get('code')
        # [加固 P3-17] verify_totp 现返回 (是否通过, 命中时间步)
        totp_ok, _ = verify_totp(secret, code)
        if totp_ok:
            cfg = SystemConfig.query.filter_by(key='totp_secret').first()
            if cfg:
                cfg.value = secret
            else:
                db.session.add(SystemConfig(key='totp_secret', value=secret))
            db.session.commit()
            invalidate_config_cache()
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
            invalidate_config_cache()
        return jsonify({"status": "success"})