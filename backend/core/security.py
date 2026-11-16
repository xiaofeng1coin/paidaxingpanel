import os
import time
import base64
import hmac
import struct
import hashlib
import secrets

# [安全修复 P0-2] 目录穿越防护函数：沙盒路径计算
# 旧实现用 startswith 前缀比较，兄弟目录(如 base=scripts 时 scripts_evil)可绕过；
# 改用 os.path.commonpath 严格判断目标必须位于 base_dir 内部。
# [加固 P3-21] abspath 升级为 realpath：解析符号链接，防止 git 仓库携带 symlink 逃逸沙盒。
def get_safe_path(base_dir, user_input):
    if not user_input:
        return None
    # 剔除首部的斜杠，防止 os.path.join 将其直接视为绝对路径绕过 base_dir
    clean_input = str(user_input).lstrip('/\\')
    base_abs = os.path.realpath(base_dir)
    target_path = os.path.realpath(os.path.join(base_abs, clean_input))
    try:
        if os.path.commonpath([target_path, base_abs]) != base_abs:
            return None
    except ValueError:
        # Windows 跨盘符等场景 commonpath 抛 ValueError，一律拒绝
        return None
    return target_path

def generate_totp_secret():
    return ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567') for _ in range(16))

def verify_totp(secret, code, last_used_step=None):
    """校验 TOTP 动态码。
    返回 (是否通过, 本次命中的时间步)。
    [加固 P3-17] last_used_step：上一次成功使用的时间步；命中步不晚于它则拒绝（防 90 秒窗口内重放同一验证码）。"""
    try:
        padding = len(secret) % 8
        if padding != 0:
            secret += '=' * (8 - padding)
        key = base64.b32decode(secret, casefold=True)
        current_time = int(time.time() / 30)
        for i in range(-1, 2):
            step = current_time + i
            msg = struct.pack(">Q", step)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            o = h[19] & 15
            token = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 1000000
            # [安全修复] 5. 使用 hmac.compare_digest 防御密码学时序攻击
            if hmac.compare_digest(f"{token:06d}", str(code).strip()):
                if last_used_step is not None and step <= last_used_step:
                    return (False, None)  # 重放拒绝
                return (True, step)
        return (False, None)
    except Exception:
        return (False, None)