import os
import time
import base64
import hmac
import struct
import hashlib
import secrets

# [安全修复] 1. 终极目录穿越防护函数，绝对安全的路径计算与沙盒约束
def get_safe_path(base_dir, user_input):
    if not user_input:
        return None
    # 剔除首部的斜杠，防止 os.path.join 将其直接视为绝对路径绕过 base_dir
    clean_input = str(user_input).lstrip('/\\')
    target_path = os.path.abspath(os.path.join(base_dir, clean_input))
    # 严格校验：无论怎么拼，最终计算出的绝对路径必须在 base_dir 内部
    if not target_path.startswith(os.path.abspath(base_dir)):
        return None
    return target_path

def generate_totp_secret():
    return ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567') for _ in range(16))

def verify_totp(secret, code):
    try:
        padding = len(secret) % 8
        if padding != 0:
            secret += '=' * (8 - padding)
        key = base64.b32decode(secret, casefold=True)
        current_time = int(time.time() / 30)
        for i in range(-1, 2):
            msg = struct.pack(">Q", current_time + i)
            h = hmac.new(key, msg, hashlib.sha1).digest()
            o = h[19] & 15
            token = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 1000000
            # [安全修复] 5. 使用 hmac.compare_digest 防御密码学时序攻击
            if hmac.compare_digest(f"{token:06d}", str(code).strip()):
                return True
        return False
    except:
        return False