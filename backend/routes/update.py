import os
import io
import sys
import json
import time
import shutil
import zipfile
import tempfile
import urllib.request
import urllib.parse
import subprocess
import threading
from flask import jsonify
from flask_login import login_required
from backend.core.paths import VERSION_FILE, BASE_DIR
from backend.extensions import socketio
from backend.runtime import SUBPROCESS_KWARGS

def init_app(app):
    @app.route('/api/update/check', methods=['GET'])
    @login_required
    def api_update_check():
        try:
            repo = os.environ.get('GITHUB_REPO')
            branch = os.environ.get('GITHUB_BRANCH', 'main')
            if not repo:
                return jsonify({"status": "error", "msg": "未配置 GITHUB_REPO 环境变量，无法检查更新"})

            if os.path.exists(VERSION_FILE):
                with open(VERSION_FILE, 'r', encoding='utf-8') as f:
                    local_data = json.load(f)
                    local_version = local_data.get('version', '1.0.0')
            else:
                local_version = '1.0.0'

            url = f"https://raw.githubusercontent.com/{repo}/{branch}/version.json?t={int(time.time())}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(req, timeout=10) as res:
                remote_data = json.loads(res.read().decode('utf-8'))
                remote_version = remote_data.get('version')
                changelog = remote_data.get('changelog', '无更新内容')

            if remote_version and remote_version != local_version:
                return jsonify({
                    "status": "update_available",
                    "local_version": local_version,
                    "remote_version": remote_version,
                    "changelog": changelog
                })
            else:
                return jsonify({"status": "no_update"})
        except Exception as e:
            return jsonify({"status": "error", "msg": f"检查更新失败: {str(e)}"})


    @app.route('/api/update/do', methods=['POST'])
    @login_required
    def api_update_do():
        repo = os.environ.get('GITHUB_REPO')
        if not repo:
            return jsonify({"status": "error", "msg": "未配置 GITHUB_REPO 环境变量，无法执行更新"})

        def _do_update():
            stream_id = "sys_update"
            is_exe = getattr(sys, 'frozen', False)
            is_magisk = bool(os.environ.get('ANDROID_DATA_DIR'))
            is_termux = bool(os.environ.get('TERMUX_DATA_DIR'))
            branch = os.environ.get('GITHUB_BRANCH', 'main')

            try:
                # 1. 重新获取一次远程的 version.json，以便拿到准确的版本号拼接 GitHub Releases 链接
                socketio.emit('log_stream', {'task_id': stream_id, 'data': f"🚀 正在连接 GitHub 解析最新版本信息...\n", 'clear': True})
                url_version = f"https://raw.githubusercontent.com/{repo}/{branch}/version.json?t={int(time.time())}"
                req_version = urllib.request.Request(url_version, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req_version, timeout=10) as res:
                    remote_data = json.loads(res.read().decode('utf-8'))
                    target_version = remote_data.get('version', '1.0.0')

                # 2. 判断不同的运行环境并执行相应的更新策略
                if is_exe:
                    # ================= EXE 自动更新逻辑 =================
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"🖥️ 检测到当前为 Windows EXE 环境，准备下载新版二进制文件...\n"})
                    
                    exe_filename = f"派大星面板-Windows-v{target_version}.exe"
                    exe_url = f"https://github.com/{repo}/releases/download/v{target_version}/{urllib.parse.quote(exe_filename)}"
                    
                    temp_dir = tempfile.mkdtemp()
                    new_exe_path = os.path.join(temp_dir, "PatrickPanel_New.exe")
                    
                    req = urllib.request.Request(exe_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=120) as res, open(new_exe_path, 'wb') as f:
                        shutil.copyfileobj(res, f)
                    
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"✅ 新版 EXE 下载完成，准备执行热替换脚本...\n"})
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"⚠️ 注意：面板即将短暂关闭并自动重启，请不要关闭当前页面，稍候...\n"})
                    time.sleep(2)

                    current_exe_path = sys.executable
                    bat_path = os.path.join(temp_dir, "update.bat")
                    with open(bat_path, "w", encoding="utf-8") as f:
                        f.write(f"""@echo off
                    echo Updating PatrickPanel... Please wait...
                    timeout /t 2 /nobreak > NUL
                    move /y "{new_exe_path}" "{current_exe_path}"
                    start "" "{current_exe_path}"
                    del "%~f0"
                    """)
                    
                    subprocess.Popen([bat_path], shell=True, **SUBPROCESS_KWARGS)
                    os._exit(0)

                elif is_magisk:
                    # ================= 面具(Magisk) 更新逻辑 =================
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"📱 检测到当前为安卓面具(Magisk)环境，正在下载模块包...\n"})
                    
                    magisk_filename = f"派大星面板-面具模块-v{target_version}.zip"
                    magisk_url = f"https://github.com/{repo}/releases/download/v{target_version}/{urllib.parse.quote(magisk_filename)}"
                    
                    download_dir = "/sdcard/Download"
                    os.makedirs(download_dir, exist_ok=True)
                    zip_path = os.path.join(download_dir, magisk_filename)
                    
                    req = urllib.request.Request(magisk_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=120) as res, open(zip_path, 'wb') as f:
                        shutil.copyfileobj(res, f)
                    
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"✅ 模块下载完成！\n\n"})
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"==============================================\n"})
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"📦 更新包已保存至: {zip_path}\n"})
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"⚠️ 面具模块无法在后台直接完成自覆盖更新。\n👉 请您打开面具(Magisk)或 KernelSU App，选择从本地安装该模块并重启手机即可完成更新！\n"})
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"==============================================\n更新失败触发标识\n"})
                    
                else:
                    # ================= 原有的源码/Docker/Termux 更新逻辑 =================
                    if is_termux:
                        socketio.emit('log_stream', {'task_id': stream_id, 'data': f"📱 检测到当前为安卓 Termux 环境，获取最新源码压缩包...\n"})
                    else:
                        socketio.emit('log_stream', {'task_id': stream_id, 'data': f"🐳 检测到当前为 Docker/源码 环境，获取最新源码压缩包...\n"})
                    
                    url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=60) as res:
                        zip_data = res.read()
                    
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"✅ 源码下载完成 ({(len(zip_data)//1024):,} KB)，正在预解压...\n"})
                    
                    temp_dir = tempfile.mkdtemp()
                    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                        zf.extractall(temp_dir)
                        
                    root_folder = os.path.join(temp_dir, os.listdir(temp_dir)[0])
                    
                    exclude_dirs = (
                        'data/', '__pycache__/', 'build/', 'develop-eggs/', 'dist/', 'downloads/', 
                        'eggs/', '.eggs/', 'lib/', 'lib64/', 'parts/', 'sdist/', 'var/', 'wheels/', 
                        'venv/', 'env/', 'ENV/', '.vscode/', '.idea/', '.git/', '.github/', 'logs/', 'docs/'
                    )
                    exclude_files = {
                        'Dockerfile', '.dockerignore', 'docker-compose.yml', 'README.md', 
                        '.gitignore', '.DS_Store', 'Thumbs.db', 'credentials.json', '.env', '.Python', '.installed.cfg'
                    }
                    exclude_exts = ('.pyc', '.pyo', '.class', '.so', '.swp', '.swo', '.pem', '.key', '.log', '.egg')

                    files_to_copy = []
                    for dirpath, dirnames, filenames in os.walk(root_folder):
                        for filename in filenames:
                            rel_path = os.path.relpath(os.path.join(dirpath, filename), root_folder)
                            rel_path_unix = rel_path.replace('\\', '/')
                            
                            if any(rel_path_unix.startswith(d) for d in exclude_dirs):
                                continue
                            if filename in exclude_files:
                                continue
                            if filename.endswith(exclude_exts) or '.egg-info/' in rel_path_unix:
                                continue
                                
                            files_to_copy.append((os.path.join(dirpath, filename), os.path.join(BASE_DIR, rel_path)))

                    files_to_copy.sort(key=lambda x: 1 if os.path.basename(x[1]) == 'app.py' else 0)

                    total_files = len(files_to_copy)
                    
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': f"📦 准备就绪，即将开始覆盖 {total_files} 个文件...\n"})
                    socketio.emit('log_stream', {'task_id': stream_id, 'data': "⚠️ 注意：覆盖完成后将触发系统自动重启，请不要关闭当前页面，稍候...\n"})
                    
                    time.sleep(2)

                    for src, dst in files_to_copy:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                    
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except:
                        pass
                    
                    time.sleep(1)
                    os._exit(0)
                
            except Exception as e:
                socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n❌ 更新失败: {str(e)}\n"})

        threading.Thread(target=_do_update, daemon=True).start()
        return jsonify({"status": "success", "msg": "已开始更新流程"})