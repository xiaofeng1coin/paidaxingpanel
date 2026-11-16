# file_utils.py 文件读取工具
# [修复 P2-5] 日志查看接口原先全量读入整个文件，日志过大会拖垮接口与浏览器；
# 统一改为读取文件尾部最多 max_bytes 的内容。
import os

DEFAULT_TAIL_BYTES = 256 * 1024


def read_text_tail(path, max_bytes=DEFAULT_TAIL_BYTES):
    """读取 UTF-8 文本文件的末尾内容；超限时截断并加提示头。非文本内容以替换字符呈现。"""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        text = data.decode('utf-8', errors='replace')
        if size > max_bytes:
            # 去掉因截断产生的首行残缺内容
            text = text.split('\n', 1)[-1] if '\n' in text else text
            text = "…… [系统提示] 日志文件过大(约 %d KB)，仅展示末尾 %d KB ……\n" % (size // 1024, max_bytes // 1024) + text
        return text
    except Exception:
        return ""
