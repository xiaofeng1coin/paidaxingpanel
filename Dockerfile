# 使用稳定且包含完整系统工具的 Python 3.11 Debian 镜像
FROM python:3.11-slim-bookworm

# 设置环境变量
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py

# 更新系统源并安装必需的基础工具，同时安装 Node.js 20.x LTS
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    cron \
    tzdata \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖清单
COPY requirements.txt .

# 安装 Python 依赖，额外加入 gunicorn 用于生产环境运行 Web 服务
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 复制项目所有代码到容器内
COPY . .

# 声明持久化数据卷（包含用户数据和动态安装的依赖环境）
VOLUME ["/app/data", "/app/deps_env"]

# 暴露 5000 端口
EXPOSE 5000

# 启动命令：使用 gunicorn 结合线程模式运行 Flask-SocketIO
CMD ["gunicorn", "--worker-class", "gthread", "--threads", "10", "--bind", "0.0.0.0:5000", "app:app"]