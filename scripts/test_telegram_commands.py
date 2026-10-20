# -*- coding: utf-8 -*-
"""实战测试 Telegram Bot 全部命令处理器（真实数据源，不启动轮询）"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from database import DatabaseManager
from config import ConfigManager
from core import DataCollector, MarketMonitor, AlertManager
from bot import TelegramBot

db = DatabaseManager(os.getenv("DATABASE_PATH", "data/database.db"))
cm = ConfigManager(db)
dc = DataCollector(cm, db)
monitor = MarketMonitor(cm, db, dc)
am = AlertManager(cm, db)
bot = TelegramBot(cm, db, monitor, am)

# 真实跑一轮采集 + 扫描（与线上主循环相同的代码路径），让命令处理器拿到真实数据
print(">>> 真实采集一轮数据（5 家交易所）...")
dc._collect_funding_rates()
dc._collect_prices()
dc._collect_stablecoins()
dc._collect_reference_prices()
print(">>> 执行一次完整扫描...")
monitor.scan_once()  # 不注册 alert 回调，避免向 Telegram 重复推送

results = []


class FakeMessage:
    def __init__(self):
        self.text = None
        self.kwargs = None

    async def reply_text(self, text, **kwargs):
        self.text = text
        self.kwargs = kwargs


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


def run_cmd(name, coro):
    upd = FakeUpdate()
    try:
        asyncio.run(coro(upd, None))
        text = upd.message.text or ""
        ok = len(text) > 10 and "Traceback" not in text
        results.append((name, ok, text))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} ({len(text)} chars)")
        print("-" * 60)
        print(text[:450])
        print()
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"[FAIL] {name}: {e}")
        print()


print("=" * 60)
print("Telegram 命令处理器实战测试（真实采集数据）")
print("=" * 60)
run_cmd("/start", bot.cmd_start)
run_cmd("/help", bot.cmd_help)
run_cmd("/status", bot.cmd_status)
run_cmd("/apr", bot.cmd_apr)
run_cmd("/spread", bot.cmd_spread)
run_cmd("/basis", bot.cmd_basis)
run_cmd("/stable", bot.cmd_stable)
run_cmd("/alerts", bot.cmd_alerts)

print("=" * 60)
passed = sum(1 for _, ok, _ in results if ok)
print(f"命令测试: {len(results)} 项 | 通过 {passed} | 失败 {len(results) - passed}")
if passed != len(results):
    sys.exit(1)
