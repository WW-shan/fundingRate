"""
Telegram Bot（监控版）
推送异常告警 + 提供查询命令（无任何交易控制功能）
"""
import os
import asyncio
import threading
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
        self._stop_requested = False
        self._pending_messages: List[str] = []
        self._pending_lock = threading.Lock()

        if not self.bot_token:
            logger.warning("TG_BOT_TOKEN not set - Telegram Bot disabled")
            return

        self._init_bot()

    def _init_bot(self):
        try:
            builder = Application.builder().token(self.bot_token)
            proxy = os.getenv('TG_PROXY') or os.getenv('PROXY_URL')
            if proxy:
                builder = builder.proxy(proxy).get_updates_proxy(proxy)
            builder = (builder.connect_timeout(20)
                              .read_timeout(30)
                              .write_timeout(30)
                              .pool_timeout(20))
            self.app = builder.build()
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
            import time
            while not self._stop_requested:
                self._init_bot()
                try:
                    self._loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._loop)
                    self._loop.run_until_complete(self.app.initialize())
                    self._loop.run_until_complete(self.app.start())
                    self._loop.run_until_complete(self.app.updater.start_polling())
                    logger.info("Telegram polling loop started")
                    self._flush_pending_messages()
                    self._loop.run_forever()
                except Exception as e:
                    logger.error(f"Telegram bot thread stopped: {e}")
                finally:
                    if self._loop and not self._loop.is_closed():
                        try:
                            self._loop.close()
                        except Exception:
                            pass
                    self._loop = None
                if not self._stop_requested:
                    logger.info("Restarting Telegram bot in 5 seconds")
                    time.sleep(5)

        threading.Thread(target=run_bot, daemon=True).start()
        logger.info("Telegram Bot started in background thread")

    def stop(self):
        self._stop_requested = True
        if not self.app or not self._loop:
            return

        async def shutdown():
            try:
                if self.app.updater and self.app.updater.running:
                    await self.app.updater.stop()
            finally:
                try:
                    await self.app.stop()
                finally:
                    await self.app.shutdown()

        try:
            future = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
            future.result(timeout=15)
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception as e:
            logger.error(f"Error stopping bot: {e}")

    def push_alert(self, payload: Dict[str, Any]):
        """告警推送（由 AlertManager 回调）

        兼容两种入参：
          - digest 字典: {'alerts': [...], 'count': N}，合并为一条汇总消息
          - 单条告警字典（旧接口）
        """
        if isinstance(payload, dict) and "alerts" in payload:
            alerts = payload.get("alerts") or []
        else:
            alerts = [payload] if payload else []

        if not alerts:
            return

        # One Telegram message per alert event. Funding alerts already merge all
        # anomalous exchanges for the same symbol; unrelated events such as
        # different depeg assets must not be mixed into one digest message.
        for alert in alerts:
            self._send_async(self._format_single_alert(alert))

    def _format_digest(self, alerts: List[Dict[str, Any]]) -> str:
        """多条告警合并为一条汇总消息"""
        crit = sum(1 for a in alerts if a.get("level") in ("critical", "emergency"))
        header = f"📊 <b>监控告警 · {len(alerts)} 条</b>"
        if crit:
            header += f"（🔴 严重 {crit}）"
        lines = [header, "━━━━━━━━━━━━━━━"]
        # Telegram 单条消息上限 4096 字符；预留余量，超长时截断并引导到面板
        for i, a in enumerate(alerts):
            line = self._format_alert_line(a)
            if len("\n".join(lines + [line])) > 3800:
                lines.append(f"\n…另有 {len(alerts) - i} 条告警，请在 Web 面板查看")
                break
            lines.append(line)
        lines.append(f"\n时间: {self._fmt_time()}")
        return "\n".join(lines)

    def _format_alert_line(self, alert: Dict[str, Any]) -> str:
        """单条告警压缩为 1-2 行（汇总消息内使用）"""
        emoji = {"warning": "⚠️", "critical": "🔴", "emergency": "🚨"}.get(alert.get("level"), "⚠️")
        t = alert.get("alert_type")
        d = alert.get("details") or {}

        if t == "funding_extreme" and d.get("exchanges"):
            legs = d.get("exchanges") or []
            count = d.get("exchange_count") or len(legs)
            detail = " | ".join(
                f"{str(x.get('exchange', '')).upper()} {((x.get('apr') or 0) * 100):+.1f}%"
                for x in legs[:6]
            )
            suffix = f" 等 {count} 所" if count > 6 else ""
            return (
                f"\n{emoji} <b>资金费率·多所</b> {d.get('symbol', 'N/A')}（{count}所）\n"
                f"   {detail}{suffix}"
            )
        if t == "funding_extreme":
            apr = d.get("apr")
            rate = d.get("funding_rate")
            interval_h = d.get("funding_interval_hours")
            interval_str = f"每{interval_h:g}h" if interval_h else "周期未知"
            return (
                f"\n{emoji} <b>资金费率</b> {d.get('symbol', 'N/A')} @ {str(d.get('exchange', '')).upper()}\n"
                f"   APR <b>{apr*100:+.1f}%</b> | 费率 {rate*100:+.4f}% | {interval_str}"
            )
        if t == "stablecoin_depeg":
            cat_label = {
                "usd_stable": "稳定币", "eur_stable": "欧元稳定币",
                "commodity": "大宗商品", "stock": "股票代币",
            }.get(d.get("category"), "锚定资产")
            reference_source = d.get("reference_source")
            if reference_source == "peer":
                reference_note = "（休市，参考同类中位价）"
            elif d.get("market_closed"):
                reference_note = "（休市参考收盘价）"
            else:
                reference_note = ""
            return (
                f"\n{emoji} <b>{cat_label}脱锚</b> {d.get('symbol', 'N/A')} @ {str(d.get('exchange', '')).upper()}\n"
                f"   {d.get('price', 0):.6g} vs {d.get('reference', 0):.6g} "
                f"(<b>{d.get('deviation', 0)*100:+.2f}%</b>){reference_note}"
            )
        if t == "issuer_spread":
            quotes = d.get("quotes", [])
            labels = []
            for q in quotes[:4]:
                issuer = q.get("issuer") or q.get("exchange")
                normalized = q.get("normalized_price", q.get("price", 0))
                labels.append(f"{q.get('symbol')}({issuer}) ${normalized:.4g}")
            detail = " | ".join(labels)
            spread_label = "跨产品价差" if d.get("category") == "stock" else "跨发行商价差"
            return (
                f"\n{emoji} <b>{spread_label}</b> {d.get('underlying', 'N/A')}\n"
                f"   价差 <b>{d.get('abs_spread', 0)*100:.2f}%</b> | {detail}"
            )
        if t == "basis_extreme":
            return (
                f"\n{emoji} <b>基差异常</b> {d.get('symbol', 'N/A')} @ {str(d.get('exchange', '')).upper()}\n"
                f"   <b>{d.get('basis', 0)*100:+.2f}%</b> "
                f"(现货 {d.get('spot_price', 0):.6g} / 期货 {d.get('futures_price', 0):.6g})"
            )
        if t == "cross_spread":
            return (
                f"\n{emoji} <b>跨所费率差</b> {d.get('symbol', 'N/A')}\n"
                f"   <b>{d.get('abs_spread', 0)*100:.1f}%</b> "
                f"({d.get('exchange_a')} vs {d.get('exchange_b')})"
            )
        return f"\n{emoji} {alert.get('message', '')}"

    def _format_single_alert(self, alert: Dict[str, Any]) -> str:
        """单条告警的详细版消息"""
        emoji = {"warning": "⚠️", "critical": "🔴", "emergency": "🚨"}.get(alert.get("level"), "⚠️")
        alert_type = alert.get("alert_type")

        if alert_type == "funding_extreme" and (alert.get("details") or {}).get("exchanges"):
            d = alert.get("details") or {}
            legs = d.get("exchanges") or []
            lines = [
                f"{emoji} <b>多所资金费率异常</b>\n",
                f"标的: <b>{d.get('symbol', 'N/A')}</b>",
                f"异常交易所: <b>{d.get('exchange_count') or len(legs)}</b>\n",
            ]
            for x in legs[:8]:
                rate = x.get('funding_rate')
                apr = x.get('apr')
                interval_h = x.get('funding_interval_hours')
                rate_str = f"{rate * 100:+.4f}%" if rate is not None else "N/A"
                apr_str = f"{apr * 100:+.1f}%" if apr is not None else "N/A"
                interval_str = f"每{interval_h:g}h" if interval_h else "周期未知"
                lines.append(
                    f"  {str(x.get('exchange', '')).upper()}: APR <b>{apr_str}</b> | "
                    f"费率 {rate_str} | {interval_str}"
                )
            top_apr = d.get('apr')
            if top_apr is not None:
                lines.append(f"\n最高年化: <b>{top_apr * 100:+.1f}%</b>")
            lines.append(f"\n时间: {self._fmt_time()}")
            message = "\n".join(lines)
        elif alert_type == "funding_extreme":
            d = alert.get("details") or {}
            apr = d.get("apr")
            rate = d.get("funding_rate")
            interval_h = d.get("funding_interval_hours")
            per_day = (24 / interval_h) if interval_h else None
            next_ts = d.get("next_funding_time")
            message = (
                f"{emoji} <b>极端资金费率</b>\n\n"
                f"币种: <b>{d.get('symbol', 'N/A')}</b>\n"
                f"交易所: <b>{str(d.get('exchange', 'N/A')).upper()}</b>\n"
                f"当前费率: <b>{rate*100:+.4f}%</b>\n"
                f"收费频率: <b>{('每 %gh（每日 %.0f 次）' % (interval_h, per_day)) if interval_h else '未知'}</b>\n"
                f"年化 APR: <b>{apr*100:+.1f}%</b>\n"
                f"费率类型: 下期将收取\n"
                f"下次结算: {self._fmt_ts(next_ts)}\n\n"
                f"时间: {self._fmt_time()}"
            )
        elif alert_type == "stablecoin_depeg":
            d = alert.get("details") or {}
            cat_label = {
                "usd_stable": "稳定币", "eur_stable": "欧元稳定币",
                "commodity": "大宗商品", "stock": "股票代币",
            }.get(d.get("category"), "锚定资产")
            if d.get("reference_source") == "peer":
                closed_note = "\n⚠️ 标的市场休市中（参考价为其他发行商同类产品中位价）"
            elif d.get("market_closed"):
                closed_note = "\n⚠️ 标的市场休市中（参考价为最近收盘价）"
            else:
                closed_note = ""
            threshold_note = (
                f"\n阈值: 预警 {d.get('threshold_warning', 0)*100:.2f}% / "
                f"严重 {d.get('threshold_critical', 0)*100:.2f}%"
            )
            message = (
                f"{emoji} <b>{cat_label}脱锚</b>\n\n"
                f"资产: <b>{d.get('symbol', 'N/A')}</b>\n"
                f"交易所: <b>{str(d.get('exchange', 'N/A')).upper()}</b>\n"
                f"当前价: <b>{d.get('price', 0):.6f}</b>\n"
                f"参考价: <b>{d.get('reference', 0):.6f}</b>\n"
                f"偏离: <b>{d.get('deviation', 0)*100:+.3f}%</b>{threshold_note}{closed_note}\n\n"
                f"时间: {self._fmt_time()}"
            )
        elif alert_type == "issuer_spread":
            d = alert.get("details") or {}
            spread_label = "跨产品价差" if d.get("category") == "stock" else "跨发行商价差"
            lines = [f"{emoji} <b>{spread_label}</b>\n",
                     f"标的: <b>{d.get('underlying', 'N/A')}</b>",
                     f"价差: <b>{d.get('abs_spread', 0)*100:.2f}%</b>",
                     f"产品/发行商数: {d.get('issuer_count', 0)} | 交易所数: {d.get('exchange_count', 0)}\n"]
            for q in d.get("quotes", [])[:8]:
                issuer = q.get("issuer") or q.get("exchange")
                normalized = q.get("normalized_price", q["price"])
                raw_note = f" [raw ${q['price']:.4f}]" if abs(normalized - q["price"]) > 1e-9 else ""
                lines.append(f"  {q['symbol']} ({issuer}): <b>${normalized:.4f}</b>{raw_note}")
            lines.append(f"\n时间: {self._fmt_time()}")
            message = "\n".join(lines)
        else:
            type_label = {
                "cross_spread": "跨所费率差",
                "basis_extreme": "基差异常",
            }.get(alert_type, alert_type)
            message = f"{emoji} <b>{type_label}</b>\n\n{alert.get('message', '')}\n\n时间: {self._fmt_time()}"
        return message

    @staticmethod
    def _fmt_ts(ms):
        if not ms:
            return 'N/A'
        from datetime import datetime
        return datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M:%S')

    def push_daily_summary(self, summary: Dict[str, Any]):
        """每日汇总推送"""
        top_apr = summary.get('top_apr', [])[:5]
        lines = ["📊 <b>每日监控汇总</b>\n"]
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
        """异步发送；loop 未就绪时先入队，启动后统一补发，避免启动窗口丢消息"""
        if not self.app or not self.chat_id:
            return
        loop = self._loop
        if loop and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._send(message), loop)
                return
            except Exception as e:
                logger.error(f"Failed to queue message: {e}")
                return
        with self._pending_lock:
            self._pending_messages.append(message)
            pending = len(self._pending_messages)
        logger.warning(f"TG loop not ready, message queued (pending={pending})")

    def _flush_pending_messages(self):
        """在 bot loop 启动后补发启动窗口内积压的消息"""
        with self._pending_lock:
            messages = self._pending_messages
            self._pending_messages = []
        for message in messages:
            try:
                self._loop.create_task(self._send(message))
            except Exception as e:
                logger.error(f"Failed to flush pending message: {e}")

    async def _send(self, message: str):
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=message, parse_mode='HTML')
            logger.info("TG message sent")
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
            "/apr - 极端 APR Top10（正负均列）\n"
            "/spread - 跨所费率差 Top10\n"
            "/basis - 基差异常 Top10\n"
            "/stable - 锚定资产状态\n"
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
            f"锚定资产: {status.get('peg_assets', 0)} 个（{status['stablecoins_tracked']} 个标的）\n"
            f"24h 异常记录: {stats['total']} 条（推送 {stats.get('pushed_total', 0)} 条）"
        )
        await update.message.reply_text(text, parse_mode='HTML')

    async def cmd_apr(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = [r for r in self.monitor.last_scan.get('apr_ranking', []) if r.get('apr') is not None]
        ranking = sorted(rows, key=lambda r: abs(r['apr']), reverse=True)[:10]
        if not ranking:
            await update.message.reply_text("暂无数据")
            return
        lines = ["📈 <b>极端 APR Top10（正负均列）</b>\n"]
        for i, row in enumerate(ranking, 1):
            apr = row.get('apr')
            apr_str = f"{apr*100:.1f}%" if apr is not None else "N/A"
            interval = row.get('funding_interval_hours')
            interval_str = f"每{interval:g}h" if interval else "周期未知"
            rate_str = f"{row['funding_rate']*100:+.4f}%" if row.get('funding_rate') is not None else "N/A"
            lines.append(
                f"{i}. {row['symbol']} @ {row['exchange']}: <b>APR {apr_str}</b> | "
                f"当前 {rate_str} | {interval_str}"
            )
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
            await update.message.reply_text("暂无锚定资产数据")
            return
        comparable = [r for r in depeg if r.get('deviation') is not None]
        skipped = len(depeg) - len(comparable)
        lines = ["🪙 <b>锚定资产状态</b>\n"]
        for row in comparable[:15]:
            # 使用各资产类别自己的阈值（稳定币/黄金/股票不同）
            crit = row.get('threshold_critical', 0.01)
            warn = row.get('threshold_warning', 0.003)
            flag = "🔴" if row['abs_deviation'] >= crit else ("⚠️" if row['abs_deviation'] >= warn else "✅")
            if row.get('reference_source') == 'peer':
                source_note = " [跨产品]" if row.get('category') == 'stock' else " [同类]"
            else:
                source_note = ""
            lines.append(
                f"{flag} {row['symbol']} @ {row['exchange']}: {row['price']:.6g} "
                f"({row['deviation']*100:+.3f}%){source_note}"
            )
        if skipped:
            lines.append(f"\n另有 {skipped} 个无同类产品可比，未做价格比较")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')

    async def cmd_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        alerts = self.alert_manager.get_recent_alerts(limit=10, hours=24)
        if not alerts:
            await update.message.reply_text("24 小时内无告警")
            return
        lines = ["🔔 <b>最近告警</b>\n"]
        for a in alerts:
            emoji = {'warning': '⚠️', 'critical': '🔴', 'emergency': '🚨'}.get(a.get('level'), '⚠️')
            lines.append(f"{emoji} {a.get('message', '')}")
        await update.message.reply_text("\n".join(lines), parse_mode='HTML')
