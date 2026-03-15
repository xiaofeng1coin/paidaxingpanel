#!/bin/bash
set -e

echo "================================================="
echo "        派大星面板 Termux 一键安装部署脚本       "
echo "================================================="

# 1. 申请存储权限
echo -e "\n[1/6] 正在请求手机存储权限，若手机弹出提示框，请务必点击「允许」..."
termux-setup-storage
sleep 3

# 2. 安装基础环境
echo -e "\n[2/6] 正在更新系统软件源并安装必备环境 (Python, Node.js, Git)..."
pkg update -y
pkg install -y python nodejs git wget curl

# 3. 克隆项目源码
echo -e "\n[3/6] 正在下载派大星面板源码..."
cd ~
if [ -d "PatrickPanel" ]; then
    echo "检测到已存在 PatrickPanel 目录，将其重命名备份..."
    mv PatrickPanel PatrickPanel_bak_$(date +%y%m%d_%H%M%S)
fi

# 【注意】：请将下方的仓库地址换成您自己的实际仓库地址
REPO_URL="https://github.com/xiaofeng1coin/paidaxingpanel.git"
git clone $REPO_URL PatrickPanel
cd PatrickPanel

# 4. 安装 Python 依赖 (过滤掉安卓不支持的 Windows/GUI 依赖)
echo -e "\n[4/6] 正在安装 Python 依赖库..."
grep -v "pywebview\|pystray\|Pillow" requirements.txt > req_termux.txt
pip install --upgrade pip
pip install --no-cache-dir -r req_termux.txt

# 5. 生成守护与启动脚本
echo -e "\n[5/6] 正在生成面板启动与守护脚本..."
cat << 'EOF' > start.sh
#!/bin/bash
# 映射到外部手机存储的目录 (方便用户使用MT管理器等直接修改脚本/查看日志)
export TERMUX_DATA_DIR="/sdcard/PatrickPanel_Data"
# 保持在 Termux 内部的私有目录 (存放依赖、数据库，避免外置存储权限不足导致崩溃)
export TERMUX_PRIVATE_DIR="$HOME/PatrickPanel/data_private"
export TZ="Asia/Shanghai"

cd ~/PatrickPanel
echo "==============================================="
echo "   🚀 派大星面板已启动，请在浏览器访问："
echo "   👉 http://127.0.0.1:5000"
echo "==============================================="

# 守护进程循环，支持面板内的自动更新热重启
while true; do
    python app.py
    echo "⚠️ 面板进程已退出或正在热更新覆盖文件，3秒后自动重新拉起..."
    sleep 3
done
EOF

chmod +x start.sh

# 6. 完成提示并启动
echo -e "\n================================================="
echo "  ✅ 部署大功告成！"
echo "  📦 您的面板外部数据已映射到: /sdcard/PatrickPanel_Data"
echo "  💡 以后若需手动启动面板，只需输入指令: ~/PatrickPanel/start.sh"
echo "================================================="
echo -e "\n[6/6] 即将为您首次拉起面板程序..."
sleep 3

./start.sh