from datetime import datetime
from flask import request, redirect, url_for, flash, jsonify
from flask_login import login_required
from backend.models import db, Env
from backend.core.template import render_template

def init_app(app):
    @app.route('/envs', methods=['GET', 'POST'])
    @login_required
    def envs():
        if request.method == 'POST':
            env_name = request.form.get('name', '').strip()

            if Env.query.filter_by(name=env_name).first():
                flash(f"添加失败：环境变量 '{env_name}' 已存在，名称必须唯一！")
                return redirect(url_for('envs'))

            now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            max_pos = db.session.query(db.func.max(Env.position)).scalar() or 0
            db.session.add(
                Env(name=env_name,
                    value=request.form.get('value'),
                    remarks=request.form.get('remarks'),
                    updated_at=now_str,
                    position=max_pos + 1)
            )
            db.session.commit()
            return redirect(url_for('envs'))

        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        keyword = request.args.get('keyword', '').strip()

        query = Env.query
        if keyword:
            query = query.filter(
                (Env.name.like(f"%{keyword}%")) |
                (Env.remarks.like(f"%{keyword}%"))
            )

        pagination = query.order_by(Env.position.asc(), Env.id.asc()).paginate(page=page, per_page=per_page, error_out=False)
        
        return render_template('envs.html', pagination=pagination, per_page=per_page, keyword=keyword)


    @app.route('/api/env/reorder', methods=['POST'])
    @login_required
    def reorder_envs():
        data = request.json
        if data and 'order' in data:
            order_list = data['order']
            for index, env_id in enumerate(order_list):
                env = Env.query.get(int(env_id))
                if env:
                    env.position = index
            db.session.commit()
        return jsonify({"status": "success"})


    @app.route('/env/edit/<int:id>', methods=['POST'])
    @login_required
    def edit_env(id):
        env = Env.query.get(id)
        if env:
            new_name = request.form.get('name', '').strip()

            existing_env = Env.query.filter_by(name=new_name).first()
            if existing_env and existing_env.id != id:
                flash(f"修改失败：环境变量 '{new_name}' 已被其他项目使用！")
                return redirect(url_for('envs'))

            env.name = new_name
            env.value = request.form.get('value')
            env.remarks = request.form.get('remarks')
            env.updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            db.session.commit()
        return redirect(url_for('envs'))


    @app.route('/api/env/toggle/<int:id>')
    @login_required
    def toggle_env(id):
        env = Env.query.get(id)
        if env:
            env.is_disabled = 1 if getattr(env, 'is_disabled', 0) == 0 else 0
            env.updated_at = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            db.session.commit()
        return redirect(url_for('envs'))


    @app.route('/env/delete/<int:id>')
    @login_required
    def delete_env(id):
        env = Env.query.get(id)
        if env: db.session.delete(env); db.session.commit()
        return redirect(url_for('envs'))