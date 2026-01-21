#!/bin/bash
# 启动资金费率套利系统

cd "$(dirname "$0")"

echo "╔════════════════════════════════════════════════════╗"
echo "║  启动资金费率套利系统                                ║"
echo "╚════════════════════════════════════════════════════╝"
echo ""

# 检查是否已经运行
if ps aux | grep "python main.py" | grep -v grep > /dev/null; then
    echo "⚠️  系统已经在运行中"
    echo ""
    ./status.sh
    exit 1
fi

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "❌ 虚拟环境不存在,请先运行:"
    echo "   python3 -m venv venv"
    echo "   source venv/bin/activate"
    echo "   pip install -r requirements.txt"
    exit 1
fi

# 激活虚拟环境并启动
echo "✅ 激活虚拟环境..."
source venv/bin/activate

echo "✅ 启动系统..."
nohup python main.py > logs/nohup.log 2>&1 &

sleep 2

# 检查启动状态
if ps aux | grep "python main.py" | grep -v grep > /dev/null; then
    echo ""
    echo "╔════════════════════════════════════════════════════╗"
    echo "║  ✅ 系统启动成功!                                   ║"
    echo "╚════════════════════════════════════════════════════╝"
    echo ""
    ./status.sh
else
    echo ""
    echo "❌ 系统启动失败,请检查日志:"
    echo "   tail -50 logs/nohup.log"
    exit 1
fi
