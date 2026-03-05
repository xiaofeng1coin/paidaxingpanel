# database.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

# =======================
# 1. auth.db (主数据库)
# =======================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

class LoginSecurity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    failed_count = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)

class SystemConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=True)

class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    login_time = db.Column(db.String(50))
    address = db.Column(db.String(100))
    ip = db.Column(db.String(50))
    device = db.Column(db.String(255))
    status = db.Column(db.String(20))

# =======================
# 2. tasks.db (任务与依赖)
# =======================
class Task(db.Model):
    __bind_key__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    command = db.Column(db.String(200), nullable=False)
    cron = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default='Stopped')
    last_run = db.Column(db.String(50), default='Never')
    last_duration = db.Column(db.String(20), default='-')
    is_disabled = db.Column(db.Integer, default=0)

class Dependency(db.Model):
    __bind_key__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    pkg_type = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='Installing')

# =======================
# 3. envs.db (环境变量)
# =======================
class Env(db.Model):
    __bind_key__ = 'envs'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    value = db.Column(db.String(500), nullable=False)
    remarks = db.Column(db.String(100))
    is_disabled = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.String(50))
    position = db.Column(db.Integer, default=0)    # 新增：用于保存拖拽后的顺序