import os
import re
import threading
from flask import request, jsonify
from flask_login import login_required
from backend.models import db, Dependency
from backend.core.template import render_template
from backend.core.dependency_manager import find_installed_dependency
from backend.services.executors import execute_dependency_cmd
from backend.extensions import socketio

def init_app(app):
    @app.route('/deps')
    @login_required
    def deps():
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        pkg_type = request.args.get('type', 'npm')
        
        pagination = Dependency.query.filter_by(pkg_type=pkg_type).order_by(Dependency.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        return render_template('deps.html', pagination=pagination, per_page=per_page, current_type=pkg_type)


    @app.route('/api/deps/install', methods=['POST'])
    @login_required
    def api_install_deps():
        pkg_type = request.form.get('type')
        package = request.form.get('package', '').strip()

        # [加固 P3-22] 包名统一校验（拒绝 '-' 开头的参数注入、pip 路径分隔符等）
        from backend.core.task_helpers import validate_package_name
        # [加固] 依赖类型白名单：防止任意类型落入 linux apt 分支执行系统命令
        if pkg_type not in ('npm', 'pip', 'linux'):
            return jsonify({"status": "error", "msg": "不支持的依赖类型"})
        if not validate_package_name(package, pkg_type):
            return jsonify({"status": "error", "msg": "依赖包名称包含非法字符，出于安全考虑拒绝执行"})

        existing_installed_dep = find_installed_dependency(package, pkg_type)
        if existing_installed_dep:
            return jsonify({"status": "success", "id": existing_installed_dep.id, "msg": f"已存在已安装依赖：{existing_installed_dep.name}"})

        dep = Dependency.query.filter_by(name=package, pkg_type=pkg_type).first()
        if not dep:
            dep = Dependency(name=package, pkg_type=pkg_type, status='Installing')
            db.session.add(dep)
        else:
            dep.status = 'Installing'
        db.session.commit()
        threading.Thread(target=execute_dependency_cmd, args=(app, dep.id, 'install')).start()
        return jsonify({"status": "success", "id": dep.id})


    @app.route('/api/deps/uninstall/<int:id>', methods=['POST'])
    @login_required
    def api_uninstall_deps(id):
        dep = db.session.get(Dependency, id)
        if not dep: return jsonify({"status": "error"})
        if dep.status == 'Error':
            db.session.delete(dep)
            db.session.commit()
            socketio.emit('dep_status', {'id': id, 'status': 'Deleted'})
            return jsonify({"status": "success", "msg": "Deleted"})

        dep.status = 'Uninstalling'
        db.session.commit()
        threading.Thread(target=execute_dependency_cmd, args=(app, dep.id, 'uninstall')).start()
        return jsonify({"status": "success"})


    @app.route('/api/deps/log/<int:id>')
    @login_required
    def api_get_deps_log(id):
        from backend.core.dependency_manager import get_dependency_log_path
        dep = db.session.get(Dependency, id)
        if not dep: return jsonify({"content": "不存在"})
        # [修复 P1-2] 统一走 sanitize 后的日志路径（scoped npm 包名含 / 会导致路径错乱）
        log_file_path = get_dependency_log_path(dep.id, dep.name)
        if os.path.exists(log_file_path):
            from backend.core.file_utils import read_text_tail
            return jsonify({"content": read_text_tail(log_file_path)})
        return jsonify({"content": "暂无日志"})