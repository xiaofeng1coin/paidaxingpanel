# proc_utils.py 进程终止工具
# [修复 P1-4] Windows 下 process.kill() 只杀主进程不杀子进程树，
# 改为统一封装：Windows 用 taskkill /F /T，POSIX 用进程组信号。
import os
import sys
import signal
import subprocess


def kill_process_tree(proc):
    """强制终止进程及其全部子进程。兼容 subprocess.Popen 对象，失败时回退 proc.kill()。"""
    if proc is None:
        return False
    try:
        if sys.platform == 'win32':
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                capture_output=True, timeout=10,
                creationflags=0x08000000  # CREATE_NO_WINDOW，避免弹出命令行窗口
            )
            return True
        else:
            # POSIX：Popen 以 start_new_session=True 启动，pgid == pid
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return True
    except Exception:
        try:
            proc.kill()
            return True
        except Exception:
            return False
