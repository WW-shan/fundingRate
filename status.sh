#!/bin/bash
# 系统状态检查脚本

echo "╔════════════════════════════════════════════════════╗"
echo "║  资金费率监控系统 - 状态检查                         ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# 检查进程
if ps aux | grep "python main.py" | grep -v grep > /dev/null; then
    PID=$(ps aux | grep "python main.py" | grep -v grep | awk '{print $2}')
    echo "✅ 系统状态: 运行中"
    echo "   进程ID: $PID"
else
    echo "❌ 系统状态: 未运行"
    exit 1
fi

# 检查日志
if [ -f "logs/app.log" ]; then
    echo ""
    echo "📊 最新日志 (最后10行):"
    echo "----------------------------------------"
    tail -10 logs/app.log | sed 's/\x1b\[[0-9;]*m//g'
fi

# 检查数据库
if [ -f "data/database.db" ]; then
    echo ""
    echo "💾 数据库: 存在"
    DB_SIZE=$(du -h data/database.db | cut -f1)
    echo "   大小: $DB_SIZE"
fi

# Git状态
echo ""
echo "📦 Git状态:"
git log --oneline -n 3

echo ""
echo "════════════════════════════════════════════════════"
echo "提示: 使用 ./stop.sh 停止系统"
echo "════════════════════════════════════════════════════"
