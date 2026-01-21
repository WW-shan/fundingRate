#!/bin/bash
# 停止资金费率套利系统

echo "╔════════════════════════════════════════════════════╗"
echo "║  停止资金费率套利系统                                ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# 查找进程
PID=$(ps aux | grep "python main.py" | grep -v grep | awk '{print $2}')

if [ -z "$PID" ]; then
    echo "⚠️  系统未运行"
    exit 0
fi

echo "🛑 正在停止系统 (PID: $PID)..."
kill $PID

# 等待进程结束
for i in {1..10}; do
    if ! ps -p $PID > /dev/null 2>&1; then
        echo ""
        echo "✅ 系统已停止"
        exit 0
    fi
    sleep 1
done

# 如果还没停止,强制kill
if ps -p $PID > /dev/null 2>&1; then
    echo "⚠️  正常停止超时,强制终止..."
    kill -9 $PID
    echo "✅ 系统已强制停止"
fi
