from flask_socketio import SocketIO
from flask_login import LoginManager
from apscheduler.schedulers.background import BackgroundScheduler

socketio = SocketIO(async_mode='threading', manage_session=False)
scheduler = BackgroundScheduler()
login_manager = LoginManager()