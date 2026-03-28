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
    source_type = db.Column(db.String(20), default='manual')
    source_key = db.Column(db.String(100), default='manual')
    source_name = db.Column(db.String(100), default='单脚本')

class Dependency(db.Model):
    __bind_key__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    pkg_type = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='Installing')

class Subscription(db.Model):
    __bind_key__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), default='public_repo')
    url = db.Column(db.String(255), nullable=False)
    alias = db.Column(db.String(50), nullable=False) # 唯一值(目录名)
    branch = db.Column(db.String(50))
    schedule_type = db.Column(db.String(20), default='crontab')
    cron = db.Column(db.String(50))
    whitelist = db.Column(db.String(255))
    blacklist = db.Column(db.String(255))
    depend_file = db.Column(db.String(255))
    extensions = db.Column(db.String(255))
    auto_add = db.Column(db.Integer, default=1)
    auto_del = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default='Idle')
    last_run = db.Column(db.String(50), default='-')
    is_disabled = db.Column(db.Integer, default=0)

class TaskView(db.Model):
    __bind_key__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    source_key = db.Column(db.String(100), nullable=False, unique=True)
    source_type = db.Column(db.String(20), default='manual')
    is_visible = db.Column(db.Integer, default=0)
    is_system = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)

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
    position = db.Column(db.Integer, default=0)