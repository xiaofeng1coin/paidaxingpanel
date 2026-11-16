# csrf.py 轻量 CSRF 防护
# [修复 P1-9] 全站 POST 原先无 CSRF token，危险操作可被跨站诱导；
# 实现：登录用户 session 内保存随机 token，前端通过 meta 标签读取，
# fetch 全局覆写自动附加 X-CSRFToken 头，表单提交附加隐藏域 csrf_token。
import secrets

from flask import session, request, jsonify
from flask_login import current_user

_FORM_FIELD = 'csrf_token'
_HEADER_NAME = 'X-CSRFToken'


def get_csrf_token():
    """获取（或首次生成）当前会话的 CSRF token，供模板注入。"""
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['_csrf_token'] = token
    return token


def verify_request_token():
    """校验当前写请求是否携带匹配的 CSRF token。"""
    token = session.get('_csrf_token')
    if not token:
        return False
    req_token = request.headers.get(_HEADER_NAME) or request.form.get(_FORM_FIELD)
    if not req_token and request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            req_token = body.get(_FORM_FIELD)
    return bool(req_token) and secrets.compare_digest(str(req_token), str(token))


def before_request_csrf():
    """注册为 before_request：
    - 已登录用户：全部写请求必须携带 CSRF token；
    - 未登录用户：登录/安装两个表单端点同样校验（防登录 CSRF），
      原生 APP 请求（带专属标识头，不存在浏览器跨站上下文）豁免。"""
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return None
    if not current_user.is_authenticated:
        # [加固 P3-18] 登录/安装表单也纳入 CSRF 保护
        if request.endpoint in ('login', 'install') and \
                request.headers.get('X-Requested-With') != 'FlutterPanelApp':
            if not verify_request_token():
                return "安全校验失败(CSRF)，请刷新页面后重试", 403
        return None
    # SocketIO 的轮询/握手请求由自身连接鉴权保护，不受影响
    if verify_request_token():
        return None
    if request.path.startswith('/api/'):
        return jsonify({"status": "error", "msg": "安全校验失败(CSRF)，请刷新页面后重试"}), 403
    return "安全校验失败(CSRF)，请刷新页面后重试", 403
