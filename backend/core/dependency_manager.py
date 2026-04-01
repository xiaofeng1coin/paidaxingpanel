import os
import re
import subprocess
from datetime import datetime
from backend.models import db, Dependency, SystemConfig
from backend.extensions import socketio
from backend.core.paths import LOGS_DIR, NODE_DIR, PYTHON_DIR, SCRIPTS_DIR
from backend.core.env_manager import get_combined_env
from backend.runtime import SUBPROCESS_KWARGS

def normalize_python_package_name(pkg_name):
    mapping = {
        'bs4': 'beautifulsoup4',
        'cv2': 'opencv-python',
        'yaml': 'PyYAML',
        'PIL': 'Pillow',
        'Crypto': 'pycryptodome',
        'OpenSSL': 'pyOpenSSL',
        'sklearn': 'scikit-learn'
    }
    return mapping.get(pkg_name, pkg_name)

def normalize_dependency_key(pkg_name, pkg_type):
    pkg_name = (pkg_name or '').strip()
    if not pkg_name:
        return ''

    if pkg_type == 'npm':
        if pkg_name.startswith('@'):
            parts = pkg_name.split('/')
            if len(parts) >= 2:
                scope = parts[0]
                name_part = parts[1]
                if '@' in name_part:
                    name_part = name_part.split('@', 1)[0]
                return f"{scope}/{name_part}"
            return pkg_name
        if '@' in pkg_name:
            return pkg_name.split('@', 1)[0]
        return pkg_name

    if pkg_type == 'pip':
        pkg_name = normalize_python_package_name(pkg_name)
        pkg_name = re.split(r'[<>=!~]', pkg_name, 1)[0].strip()
        return pkg_name.lower().replace('_', '-')

    return pkg_name

def find_installed_dependency(pkg_name, pkg_type):
    target_key = normalize_dependency_key(pkg_name, pkg_type)
    if not target_key:
        return None

    deps = Dependency.query.filter_by(pkg_type=pkg_type).all()
    for dep in deps:
        if dep.status != 'Installed':
            continue
        dep_key = normalize_dependency_key(dep.name, pkg_type)
        if dep_key == target_key:
            if pkg_type == 'npm':
                dep_path = os.path.join(NODE_DIR, 'node_modules', target_key)
                if not os.path.exists(dep_path):
                    continue
            return dep
    return None

def upsert_dependency_record(pkg_name, pkg_type, status):
    dep = Dependency.query.filter_by(name=pkg_name, pkg_type=pkg_type).first()
    if not dep:
        dep = Dependency(name=pkg_name, pkg_type=pkg_type, status=status)
        db.session.add(dep)
    else:
        dep.status = status
    db.session.commit()
    return dep

def run_dependency_install_sync(app, pkg_name, pkg_type, log_writer=None):
    try:
        if pkg_type not in ['npm', 'pip']:
            return False, "不支持的依赖类型"

        if not pkg_name or not re.match(r'^[A-Za-z0-9_\-\.\@\/=]+$', pkg_name):
            return False, "依赖名包含非法字符"

        existing_installed_dep = find_installed_dependency(pkg_name, pkg_type)
        if existing_installed_dep:
            msg = f"检测到已安装同名依赖，跳过重复安装：当前请求 [{pkg_name}]，已安装记录 [{existing_installed_dep.name}]"
            if log_writer:
                log_writer(msg + "\n")
            return True, msg

        node_mirror = SystemConfig.query.filter_by(key='node_mirror').first()
        python_mirror = SystemConfig.query.filter_by(key='python_mirror').first()

        dep = upsert_dependency_record(pkg_name, pkg_type, 'Installing')
        socketio.emit('dep_status', {'id': dep.id, 'status': dep.status})

        dep_log_dir = os.path.join(LOGS_DIR, 'dependencies')
        os.makedirs(dep_log_dir, exist_ok=True)
        log_file_path = os.path.join(dep_log_dir, f"dep_{dep.id}_{dep.name}.log")

        if pkg_type == 'npm':
            run_cwd = NODE_DIR
            cmd = f"npm install {pkg_name}"
            if node_mirror and node_mirror.value:
                cmd += f" --registry={node_mirror.value}"
        else:
            run_cwd = PYTHON_DIR
            cmd = f"pip install --target={PYTHON_DIR} {pkg_name}"
            if python_mirror and python_mirror.value:
                cmd += f" -i {python_mirror.value}"

        start_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start_msg = f"==============================================\n" \
                    f"🚀 开始自动安装依赖 | 时间: {start_time_str}\n" \
                    f"👉 依赖类型: {pkg_type}\n" \
                    f"👉 依赖名称: {pkg_name}\n" \
                    f"👉 执行指令: {cmd}\n" \
                    f"==============================================\n\n"

        if log_writer:
            log_writer(start_msg)

        with open(log_file_path, 'w', encoding='utf-8') as f:
            f.write(start_msg)
            f.flush()

            process = subprocess.Popen(cmd, shell=True, env=get_combined_env(app), stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, cwd=run_cwd, text=True, bufsize=1,
                                       encoding='utf-8', errors='replace', **SUBPROCESS_KWARGS)
            install_output = []
            for line in process.stdout:
                install_output.append(line)
                f.write(line)
                f.flush()
                if log_writer:
                    log_writer(line)
            process.wait()

            end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            end_msg = f"\n==============================================\n" \
                      f"✅ 自动安装结束 | 时间: {end_time_str}\n" \
                      f"🛑 退出码: {process.returncode}\n" \
                      f"==============================================\n"
            f.write(end_msg)
            if log_writer:
                log_writer(end_msg)

        install_success = process.returncode == 0

        if install_success and pkg_type == 'npm':
            verify_cmd = ['node', '-e', f"require('{normalize_dependency_key(pkg_name, 'npm')}'); console.log('ok')"]
            verify_proc = subprocess.run(
                verify_cmd,
                cwd=SCRIPTS_DIR,
                env=get_combined_env(app),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                **SUBPROCESS_KWARGS
            )
            if verify_proc.returncode != 0:
                install_success = False
                with open(log_file_path, 'a', encoding='utf-8') as f:
                    f.write("\n[依赖校验失败] 安装命令返回成功，但实际 require 失败：\n")
                    f.write(verify_proc.stdout or '')
                    f.write(verify_proc.stderr or '')

        dep.status = 'Installed' if install_success else 'Error'
        db.session.commit()
        socketio.emit('dep_status', {'id': dep.id, 'status': dep.status})

        if install_success:
            return True, ''.join(install_output)
        return False, ''.join(install_output)
    except Exception as e:
        try:
            dep = Dependency.query.filter_by(name=pkg_name, pkg_type=pkg_type).first()
            if dep:
                dep.status = 'Error'
                db.session.commit()
                socketio.emit('dep_status', {'id': dep.id, 'status': dep.status})
        except:
            pass
        return False, str(e)