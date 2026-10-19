"""
Telegram Bot（监控版）
推送异常告警 + 提供查询命令（无任何交易控制功能）
"""
import os
import asyncio
from typing import Any, Dict, List
from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


class TelegramBot:
    """Telegram 监控推送 Bot"""

    def __init__(self, config_manager, db_manager, market_monitor, alert_manager):
        self.config = config_manager
        self.db = db_manager
        self.monitor = market_monitor
        self.alert_manager = alert_manager
        self.bot_token = os.getenv('TG_BOT_TOKEN')
        self.chat_id = os.getenv('TG_CHAT_ID')
        self.app = None
        self._loop = None

        if not self.bot_token:
            logger.warning("TG_BOT_TOKEN not set - Telegram Bot disabled")
            return

        self._init_bot()

    def _init_bot(self):
        try:
            self.app = Application.builder().token(self.bot_token).build()
            self.app.add_handler(CommandHandler("start", self.cmd_start))
            self.app.add_handler(CommandHandler("help", self.cmd_help))
            self.app.add_handler(CommandHandler("status", self.cmd_status))
            self.app.add_handler(CommandHandler("apr", self.cmd_apr))
            self.app.add_handler(CommandHandler("spread", self.cmd_spread))
            self.app.add_handler(CommandHandler("basis", self.cmd_basis))
            self.app.add_handler(CommandHandler("stable", self.cmd_stable))
            self.app.add_handler(CommandHandler("alerts", self.cmd_alerts))
            logger.info("Telegram Bot initialized (monitor mode)")
        except Exception as e:
            logger.error(f"Failed to initialize Telegram Bot: {e}")

    def start(self):
        if not self.app:
            return

        def run_bot():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.app.initialize())
            self._loop.run_until_complete(self.app.start())
            self._loop.run_until_complete(self.app.updater.start_polling())
            self._loop.run_forever()

        import threading
        threading.Thread(target=run_bot, daemon=True).start()
        logger.info("Telegram Bot started in background thread")

    def stop(self):
        if self.app and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self.app.updater.stop(), self._loop)
                asyncio.run_coroutine_threadsafe(self.app.stop(), self._loop)
                asyncio.run_coroutine_threadsafe(self.app.shutdown(), self._loop)
            except Exception as e:
                logger.error(f"Error stopping bot: {e}")

    # ---------- 推送 ----------

    def push_alert(self, alert: Dict[str, Any]):
        """告警推送（由 AlertManager 回调）"""
        emoji = {'warning': '⚠️', 'critical': '🔴', 'emergency': '🚨'}.get(alert.get('level'), '⚠️')
        alert_type = alert.get('alert_type')

        if alert_type == 'funding_extreme':
            d = alert.get('details') or {}
            apr = d.get('apr')
            rate = d.get('funding_rate')
            interval_h = d.get('funding_interval_hours')
            per_day = (24 / interval_h) if interval_h else None
            semantics = '上期已结算' if d.get('rate_semantics') == 'last_settled' else '下期将收取'
            next_ts = d.get('next_funding_time')

            message = (
                f"{emoji} <b>极端资金费率</b>\n\n"
                f"币种: <b>{d.get('symbol', 'N/A')}</b>\n"
                f"交易所: <b>{str(d.get('exchange', 'N/A')).upper()}</b>\n"
                f"当前费率: <b>{rate*100:+.4f}%</b>\n"
                f"收费频率: <b>{('每 %gh（每日 %.0f 次）' % (interval_h, per_day)) if interval_h else '未知'}</b>\n"
                f"年化 APR: <b>{apr*100:+.1f}%</b>\n"
                f"费率类型: {semantics}\n"
                f"下次结算: {self._fmt_ts(next_ts)}\n\n"
                f"时间: {self._fmt_time()}"
            )
        elif alert_type == 'stablecoin_depeg':
            d = alert.get('details') or {}
            cat_label = {
                'usd_stable': '稳定币',
                'eur_stable': '欧元稳定币',
                'gold': '黄金代币',
                'stock': '股票代币',
            }.get(d.get('category'), '锚定资产')
            closed_note = '\n⚠️ 标的市场休市中（参考价为最近收盘价）' if d.get('market_closed') else ''
            message = (
                f"{emoji} <b>{cat_label}脱锚</b>\n\n"
                f"资产: <b>{d.get('symbol', 'N/A')}</b>\n"
                f"交易所: <b>{str(d.get('exchange', 'N/A')).upper()}</b>\n"
                f"当前价: <b>{d.get('price', 0):.6f}</b>\n"
                f"参考价: <b>{d.get('reference', 0):.6f}</b>\n"
                f"偏离: <b>{d.get('deviation', 0)*100:+.3f}%</b>{closed_note}\n\n"
                f"时间: {self._fmt_time()}"
            )
        else:
            type_label = {
                'cross_spread': '跨所费率差',
                'basis_extreme': '基差异常',
            }.get(alert_type, alert_type)
            message = f"{emoji} <b>{type_label}</b>\n\n{alert.get('message', '')}\n\n时间: {self._fmt_time()}"

        self._send_async(message)

    @staticmethod
    def _fmt_ts(ms):
        if not ms:
            return 'N/A'
        from datetime import datetime
        return datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M:%S')

    def push_daily_summary(self, summary: Dict[str, Any]):
        """每日汇总推送"""
        top_apr = summary.get('top_apr', [])[:5]
        lines = [f"📊 <b>每日监控汇总</b>\n"]
        if top_apr:
            lines.append("<b>APR 排行 Top5</b>")
            for i, row in enumerate(top_apr, 1):
                apr = row.get('apr')
                apr_str = f"{apr*100:.1f}%" if apr is not None else "N/A"
                lines.append(f"{i}. {row['symbol']} @ {row['exchange']}: {apr_str}")
        stats = summary.get('alert_stats', {})
        lines.append(f"\n24h 告警: {stats.get('total', 0)} 条")
        self._send_async("\n".join(lines))

    def _send_async(self, message: str):
        if not self.app or not self.chat_id or not self._loop:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(message), self._loop)
        except Exception as e:
            logger.error(f"Failed to queue message: {e}")

    async def _send(self, message: str):
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Failed to send TG message: {e}")

    @staticmethod
    def _fmt_time():
        from datetime import datetime
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # ---------- 命令 ----------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 资金费率监控系统\n\n使用 /help 查看可用命令。"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "📊 <b>查询命令</b>\n"
            "/apr - APR 排行 Top10\n"
            "/spread - 跨所费率差 Top10\n"
            "/basis - 基差异常 Top10\n"
            "/stable - 稳定币锚定状态\n"
            "/alerts - 最近告警\n"
            "/status - 系统状态\n\n"
            "🔔 系统会自动推送异常告警"
        )
        await update.message.reply_text(text, parse_mode='HTML')

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = self.monitor.data_collector.get_status()
        stats = self.alert_manager.get_alert_stats(24)
        text = (
            f"🖥 <b>系统状态</b>\n\n"
            f"运行中: {'✅' if status['running'] else '❌'}\n"
            f"已连接交易所: {', '.join(status['exchanges']) or '无'}\n"
            f"监控币种: {status['symbols_tracked']}\n"
            f"稳定币: {status['stablecoins_tracked']}\n"
            f"24h 告警: {stats['total']} 条"
        )
        await update.message.reply_text(text, parse_mode='HTML')

    async def cmd_apr(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        ranking = self.monitor.last_scan.get('apr_ranking', [])[:10]
        if not ranking:
            await update.message.reply_text("暂无数据")
            return
        lines = ["📈 <b>APR 排行 Top10</b>\n"]
        for i, row in enumerate(ranking, 1):
            apr = row.get('apr')
            apr_str = f"{apr*100:.1f}%" if apr is not None else "N/A"
            interval = row.get('funding_interval_hours')
            interval_str = f"每{interval:g}h" if interval else "周期未知"
            lines.append(f"{i}. {row['symbol']} @ {row['exchange']}: <b>{apr_str}</b> ({interval_str})")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    async def cmd_spread(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        spreads = self.monitor.last_scan.get('cross_spreads', [])[:10]
        if not spreads:
            await update.message.reply_text("暂无数据")
            return
        lines = ["🔀 <b>跨所费率差 Top10</b>\n"]
        for i, row in enumerate(spreads, 1):
            lines.append(
                f"{i}. {row['symbol']}: <b>{row['abs_spread']*100:.1f}%</b> "
                f"({row['exchange_a']} vs {row['exchange_b']})"
            )
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    async def cmd_basis(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        basis = self.monitor.last_scan.get('basis', [])[:10]
        if not basis:
            await update.message.reply_text("暂无数据（需现货价格采集）")
            return
        lines = ["📉 <b>基差 Top10</b>\n"]
        for i, row in enumerate(basis, 1):
            lines.append(f"{i}. {row['symbol']} @ {row['exchange']}: <b>{row['basis']*100:.2f}%</b>")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    async def cmd_stable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        depeg = self.monitor.last_scan.get('depeg', [])
        if not depeg:
            await update.message.reply_text("暂无稳定币数据")
            return
        lines = ["🪙 <b>稳定币锚定状态</b>\n"]
        for row in depeg[:15]:
            flag = "🔴" if row['abs_deviation'] >= 0.01 else ("⚠️" if row['abs_deviation'] >= 0.003 else "✅")
            lines.append(
                f"{flag} {row['symbol']} @ {row['exchange']}: {row['price']:.6f} "
                f"({row['deviation']*100:+.3f}%)"
            )
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    async def cmd_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        alerts = self.alert_manager.get_recent_alerts(limit=10, hours=24)
        if not alerts:
            await update.message.reply_text("24 小时内无告警")
            return
        lines = ["🔔 <b>最近告警</b>\n"]
        for a in alerts:
            emoji = {'warning': '⚠️', 'critical': '🔴', 'emergency': '🚨'}.get(a['level'], '⚠️')
            lines.append(f"{emoji} {a['message']}")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
