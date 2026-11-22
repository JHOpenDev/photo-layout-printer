#!/bin/zsh
# 照片排版打印系统 - 启动脚本
# 用法：双击本文件（macOS 会用终端打开）
#   · 自动启动本地服务并打开排版页面
#   · 关闭本窗口（或按 Ctrl+C）即停止服务
#   · 重复双击不会冲突：启动前会自动清理旧实例

PROJ="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ" || exit 1

cleanup(){
  kill $SRV 2>/dev/null
  pkill -f "${PROJ}/server.py" 2>/dev/null
  pkill -f "python3 server.py" 2>/dev/null
}
trap cleanup INT TERM EXIT

# 清掉之前残留的实例，保证本窗口就是服务本体
pkill -f "${PROJ}/server.py" 2>/dev/null
pkill -f "python3 server.py" 2>/dev/null
sleep 0.3

( sleep 0.8; open "http://127.0.0.1:8791" ) &
echo "────────────────────────────────────"
echo " 照片排版打印服务已在本窗口启动"
echo " · 排版页面: http://127.0.0.1:8791"
echo " · 关闭本窗口 = 停止服务"
echo "────────────────────────────────────"
python3 "$PROJ/server.py" &
SRV=$!
wait $SRV
