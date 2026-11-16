import os
import re
import shutil
import logging
import subprocess
from datetime import datetime
from backend.models import db, Dependency, SystemConfig
from backend.extensions import socketio
from backend.core.paths import LOGS_DIR, NODE_DIR, PYTHON_DIR, SCRIPTS_DIR
from backend.core.env_manager import get_combined_env
from backend.core.task_helpers import sanitize_dep_log_name, validate_package_name, sanitize_mirror_url
from backend.runtime import SUBPROCESS_KWARGS

logger = logging.getLogger('dependency')

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


def get_dependency_log_path(dep_id, dep_name):
    """[修复 P1-2] 依赖日志路径统一走这里：scoped npm 包名(含 /)会被替换成双下划线，
    否则 Windows 下 open() 直接抛错、依赖状态卡死在 Installing。"""
    log_dir = os.path.join(LOGS_DIR, 'dependencies')
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"dep_{dep_id}_{sanitize_dep_log_name(dep_name)}.log")


def uninstall_pip_target_package(pkg_name):
    """[修复 P1-1] pip --target 安装的包，pip uninstall 不作用于目标目录，文件全部残留；
    改为解析 dist-info/RECORD 精确删除安装到 PYTHON_DIR 的文件与目录。
    [加固 P1-5] RECORD 内容由被安装的包自己写入，恶意包可写 `../xxx` 条目让卸载删掉依赖环境；
    因此逐条过滤相对上跳/绝对路径，并对最终路径做 realpath 沙盒复核。"""
    base = normalize_python_package_name(pkg_name)
    base_l = base.lower()
    base_abs = os.path.realpath(PYTHON_DIR)
    removed = 0
    for entry in os.listdir(PYTHON_DIR):
        entry_path = os.path.join(PYTHON_DIR, entry)
        if not entry.lower().startswith(base_l + '-') or not entry.lower().endswith('.dist-info'):
            continue
        record_file = os.path.join(entry_path, 'RECORD')
        if os.path.exists(record_file):
            top_entries = set()
            with open(record_file, 'r', encoding='utf-8', errors='ignore') as rf:
                for line in rf:
                    rel_path = line.split(',')[0].strip().replace('\\', '/')
                    if not rel_path:
                        continue
                    top = rel_path.split('/')[0]
                    # [加固 P1-5] 拒绝空段（POSIX 绝对路径 /abs/x 与 UNC //x/y 的首段为空串，
                    # 空串拼回 PYTHON_DIR 本身会通过沙盒复核进而删光依赖环境）、
                    # 上跳目录、当前目录与盘符/绝对路径条目
                    if (not top or top in ('..', '.') or top.startswith('/')
                            or (len(top) >= 2 and top[1] == ':')):
                        continue
                    target = os.path.join(PYTHON_DIR, top)
                    # [加固 P1-5] realpath 复核：最终路径必须仍位于 PYTHON_DIR 内
                    try:
                        if os.path.commonpath([os.path.realpath(target), base_abs]) != base_abs:
                            continue
                    except ValueError:
                        continue
                    top_entries.add(top)
            for top in top_entries:
                target = os.path.join(PYTHON_DIR, top)
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                elif os.path.exists(target):
                    try:
                        os.remove(target)
                    except Exception:
                        pass
                removed += 1
        shutil.rmtree(entry_path, ignore_errors=True)
        removed += 1
    return removed > 0

def run_dependency_install_sync(app, pkg_name, pkg_type, log_writer=None):
    try:
        if pkg_type not in ['npm', 'pip']:
            return False, "不支持的依赖类型"

        # [加固 P3-22] 包名统一走白名单校验（拒绝 '-' 开头的参数注入等）
        if not validate_package_name(pkg_name, pkg_type):
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

        log_file_path = get_dependency_log_path(dep.id, dep.name)

        # [加固 P2-16] 命令改为 shell=False 参数列表（路径含空格也安全），镜像源仅接受合法 http(s) 地址
        if pkg_type == 'npm':
            run_cwd = NODE_DIR
            cmd_list = ['npm', 'install', pkg_name.strip()]
            if node_mirror and node_mirror.value:
                mirror = sanitize_mirror_url(node_mirror.value)
                if mirror:
                    cmd_list += ['--registry', mirror]
        else:
            run_cwd = PYTHON_DIR
            cmd_list = ['pip', 'install', '--target', PYTHON_DIR, pkg_name.strip()]
            if python_mirror and python_mirror.value:
                mirror = sanitize_mirror_url(python_mirror.value)
                if mirror:
                    cmd_list += ['-i', mirror]
        cmd = ' '.join(cmd_list)

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

            process = subprocess.Popen(cmd_list, shell=False, env=get_combined_env(app), stdout=subprocess.PIPE,
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
        except Exception:
            logger.warning("依赖安装异常后状态回写失败: %s", pkg_name, exc_info=True)
        logger.error("依赖安装流程异常: %s (%s)", pkg_name, e, exc_info=True)
        return False, str(e)