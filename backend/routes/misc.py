import os
from flask import request, redirect, url_for, flash, send_file, jsonify
from flask_login import login_required
from backend.core.paths import CONFIG_DIR
from backend.core.csrf import get_csrf_token

# [修复 P2-8] 头像上传：限定类型（png/jpg 魔数校验）与大小
_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_MAGIC_HEADERS = (
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpg'),
)


def _detect_image_type(head):
    for magic, ext in _MAGIC_HEADERS:
        if head.startswith(magic):
            return ext
    return None


def init_app(app):
    # [修复 A-1] CSRF token 下发接口：供配套 APP 客户端登录后获取会话 token，
    # APP 后续所有写请求携带 X-CSRFToken 头。GET 请求无需 CSRF 校验，不会死循环。
    @app.route('/api/csrf')
    @login_required
    def api_csrf():
        return jsonify({"status": "success", "csrf_token": get_csrf_token()})

    @app.route('/api/avatar')
    @login_required
    def get_avatar():
        # [加固 P3-27] 头像接口补登录校验（登录页不展示头像，无匿名使用点）
        for ext in ('png', 'jpg'):
            avatar_path = os.path.join(CONFIG_DIR, f'avatar.{ext}')
            if os.path.exists(avatar_path):
                return send_file(avatar_path)
        return "Not Found", 404


    @app.route('/api/avatar/upload', methods=['POST'])
    @login_required
    def upload_avatar():
        redirect_target = url_for('settings') + '?tab=security'
        if 'file' not in request.files:
            flash('未选择文件')
            return redirect(redirect_target)
        file = request.files['file']
        if not file or file.filename == '':
            flash('未选择文件')
            return redirect(redirect_target)

        head = file.stream.read(8)
        file.stream.seek(0)
        img_type = _detect_image_type(head)
        if not img_type:
            flash('仅支持上传 PNG 或 JPG 格式的图片')
            return redirect(redirect_target)

        data = file.read()
        if len(data) > _MAX_AVATAR_BYTES:
            flash('图片大小不能超过 2MB')
            return redirect(redirect_target)

        # 清掉另一种扩展名的旧头像，避免读取到过期文件
        for ext in ('png', 'jpg'):
            old_path = os.path.join(CONFIG_DIR, f'avatar.{ext}')
            if old_path != os.path.join(CONFIG_DIR, f'avatar.{img_type}') and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass

        try:
            file.save(os.path.join(CONFIG_DIR, f'avatar.{img_type}'))
            flash('头像已成功更新')
        except Exception as e:
            flash(f'头像保存失败: {e}')
        return redirect(redirect_target)
