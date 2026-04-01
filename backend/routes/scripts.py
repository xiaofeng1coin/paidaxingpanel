import os
import ast
import tempfile
import subprocess
import threading
import re
import time
from flask import request, redirect, url_for, jsonify
from flask_login import login_required
from werkzeug.utils import secure_filename
from backend.core.template import render_template
from backend.core.paths import SCRIPTS_DIR
from backend.core.security import get_safe_path
from backend.runtime import SUBPROCESS_KWARGS, debug_processes
from backend.services.executors import execute_debug
from backend.extensions import socketio

def init_app(app):
    @app.route('/scripts', methods=['GET', 'POST'])
    @login_required
    def scripts_editor():
        tree = {}
        all_files = []

        for root, dirs, files in os.walk(SCRIPTS_DIR):
            if '.git' in root or '__pycache__' in root:
                continue
            rel_dir = os.path.relpath(root, SCRIPTS_DIR).replace('\\', '/')
            if rel_dir == '.': rel_dir = '根目录'
            
            valid_files = [f for f in files if f.endswith(('.js', '.py', '.sh', '.json', '.txt'))]
            if valid_files:
                tree[rel_dir] = sorted(valid_files)
                for f in sorted(valid_files):
                    all_files.append(f if rel_dir == '根目录' else f"{rel_dir}/{f}")
                
        sorted_tree = {'根目录': tree.pop('根目录', [])}
        sorted_tree.update(dict(sorted(tree.items())))

        default_file = all_files[0] if all_files else 'new_script.py'
        raw_current_file = request.args.get('file', default_file)
        
        target_path = get_safe_path(SCRIPTS_DIR, raw_current_file)
        if not target_path:
            target_path = os.path.join(SCRIPTS_DIR, default_file)
            
        current_file = os.path.relpath(target_path, SCRIPTS_DIR).replace('\\', '/')
        current_folder = os.path.dirname(current_file).replace('\\', '/') or '根目录'
        current_file_name = os.path.basename(current_file)

        content = ""
        if request.method == 'POST':
            raw_filename = request.form.get('filename')
            if raw_filename:
                save_path = get_safe_path(SCRIPTS_DIR, raw_filename)
                if save_path:
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, 'w', encoding='utf-8') as f:
                        f.write(request.form.get('content').replace('\r\n', '\n'))
                    return redirect(url_for('scripts_editor', file=os.path.relpath(save_path, SCRIPTS_DIR).replace('\\', '/')))

        if os.path.exists(target_path) and os.path.isfile(target_path):
            try:
                with open(target_path, 'r', encoding='utf-8') as f: content = f.read()
            except UnicodeDecodeError:
                content = "// ⚠️ 无法读取该文件内容，它可能不是标准的 UTF-8 文本编码文件。"
        else:
            content = ""
            
        return render_template('scripts.html', tree=sorted_tree, current_folder=current_folder, current_file=current_file, current_file_name=current_file_name, content=content)


    @app.route('/api/scripts/save', methods=['POST'])
    @login_required
    def api_scripts_save():
        raw_filename = request.form.get('filename')
        content = request.form.get('content', '').replace('\r\n', '\n')
        if not raw_filename: return jsonify({"status": "error", "msg": "文件名为空"})
        
        save_path = get_safe_path(SCRIPTS_DIR, raw_filename)
        if not save_path: return jsonify({"status": "error", "msg": "非法路径禁止保存"})
            
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "msg": str(e)})


    @app.route('/api/scripts/upload', methods=['POST'])
    @login_required
    def api_scripts_upload():
        if 'file' not in request.files:
            return jsonify({"status": "error", "msg": "未选择文件"})

        file = request.files['file']
        if not file or file.filename == '':
            return jsonify({"status": "error", "msg": "未选择文件"})

        original_name = (file.filename or '').strip().replace('\\', '/').split('/')[-1].strip()
        if not original_name:
            return jsonify({"status": "error", "msg": "文件名无效"})

        ext = os.path.splitext(original_name)[1].lower()
        if ext not in ['.js', '.py', '.sh']:
            return jsonify({"status": "error", "msg": "仅支持上传 js、py、sh 文件"})

        if '\x00' in original_name:
            return jsonify({"status": "error", "msg": "文件名包含非法字符"})

        safe_name = secure_filename(original_name)
        final_name = original_name if original_name not in ['.', '..'] else safe_name
        if not final_name:
            final_name = safe_name

        if not final_name:
            return jsonify({"status": "error", "msg": "文件名无效"})

        save_path = get_safe_path(SCRIPTS_DIR, final_name)
        if not save_path:
            return jsonify({"status": "error", "msg": "非法路径禁止上传"})

        if os.path.dirname(save_path) != os.path.abspath(SCRIPTS_DIR):
            return jsonify({"status": "error", "msg": "上传文件只能保存到 scripts 根目录"})

        try:
            file.save(save_path)
            return jsonify({"status": "success", "msg": "上传成功", "filename": final_name})
        except Exception as e:
            return jsonify({"status": "error", "msg": f"上传失败: {str(e)}"})


    @app.route('/scripts/debug', methods=['GET'])
    @login_required
    def scripts_debug():
        files = []
        for root, dirs, f_names in os.walk(SCRIPTS_DIR):
            if '.git' in root or '__pycache__' in root:
                continue
            for f in f_names:
                if f.endswith(('.js', '.py', '.sh', '.json', '.txt')):
                    files.append(os.path.relpath(os.path.join(root, f), SCRIPTS_DIR).replace('\\', '/'))
                    
        files = sorted(files)
        raw_current_file = request.args.get('file', files[0] if files else 'new_script.py')
        
        target_path = get_safe_path(SCRIPTS_DIR, raw_current_file)
        if not target_path: target_path = os.path.join(SCRIPTS_DIR, 'new_script.py')
        
        current_file = os.path.relpath(target_path, SCRIPTS_DIR).replace('\\', '/')

        content = ""
        if os.path.exists(target_path) and os.path.isfile(target_path):
            try:
                with open(target_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            except UnicodeDecodeError:
                content = "// ⚠️ 无法读取该文件内容。"
        else:
            content = f"// ⚠️ 文件不存在"
            
        return render_template('debug.html', files=files, current_file=current_file, content=content)


    @app.route('/api/scripts/debug_run', methods=['POST'])
    @login_required
    def api_debug_run():
        raw_filename = request.form.get('filename')
        if not raw_filename: return jsonify({"status": "error"})

        stream_id = f"debug_{int(time.time())}"
        threading.Thread(target=execute_debug, args=(app, raw_filename, stream_id), daemon=True).start()
        return jsonify({"status": "success", "stream_id": stream_id})


    @app.route('/api/scripts/debug_stop', methods=['POST'])
    @login_required
    def api_debug_stop():
        stream_id = request.form.get('stream_id')
        if not stream_id: return jsonify({"status": "error"})
        
        if stream_id in debug_processes:
            try:
                debug_processes[stream_id].kill()
            except:
                pass
            finally:
                debug_processes.pop(stream_id, None)
                socketio.emit('log_stream', {'task_id': stream_id, 'data': f"\n🛑 手动停止\n"})
        return jsonify({"status": "success"})


    @app.route('/api/scripts/check', methods=['POST'])
    @login_required
    def check_script_syntax():
        raw_filename = request.form.get('filename', '')
        content = request.form.get('content', '')
        if not raw_filename or not content.strip():
            return jsonify({"status": "ok", "msg": ""})

        filename = os.path.basename(raw_filename)

        try:
            if filename.endswith('.py'):
                ast.parse(content)
                return jsonify({"status": "ok", "msg": "Python 语法无误"})
            elif filename.endswith('.js'):
                with tempfile.NamedTemporaryFile(suffix='.js', delete=False, mode='w', encoding='utf-8') as f:
                    f.write(content)
                    temp_name = f.name
                try:
                    result = subprocess.run(['node', '--check', temp_name], capture_output=True, text=True, **SUBPROCESS_KWARGS)
                    if result.returncode == 0:
                        return jsonify({"status": "ok", "msg": "Node.js 语法无误"})
                    else:
                        err_lines = result.stderr.split('\n')
                        line_num = 1
                        match = re.search(rf"{re.escape(temp_name)}:(\d+)", result.stderr)
                        if match: line_num = int(match.group(1))
                        err = err_lines[0] if err_lines else "JS 语法错误"
                        return jsonify(
                            {"status": "error", "msg": f"第 {line_num} 行错误: {err.replace(temp_name, filename)}",
                         "line": line_num})
                finally:
                    os.remove(temp_name)
            elif filename.endswith('.sh'):
                with tempfile.NamedTemporaryFile(suffix='.sh', delete=False, mode='w', encoding='utf-8') as f:
                    f.write(content)
                    temp_name = f.name
                try:
                    result = subprocess.run(['bash', '-n', temp_name], capture_output=True, text=True, **SUBPROCESS_KWARGS)
                    if result.returncode == 0:
                        return jsonify({"status": "ok", "msg": "Shell 语法无误"})
                    else:
                        err = result.stderr.strip()
                        match = re.search(r'line (\d+):', err)
                        line_num = int(match.group(1)) if match else 1
                        return jsonify({"status": "error", "msg": err.replace(temp_name, filename), "line": line_num})
                finally:
                    os.remove(temp_name)
            else:
                return jsonify({"status": "ok", "msg": "该文件类型暂无语法校验功能"})
        except SyntaxError as e:
            return jsonify({"status": "error", "msg": f"第 {e.lineno} 行错误: {e.msg}", "line": e.lineno})
        except Exception as e:
            return jsonify({"status": "error", "msg": f"检查异常: {str(e)}", "line": 1})