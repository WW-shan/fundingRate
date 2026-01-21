"""
Telegram Bot
提供推送通知和远程控制功能
"""
import os
from typing import Any, Dict
from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


class TelegramBot:
    """Telegram Bot"""

    def __init__(self, config_manager, db_manager, strategy_executor):
        self.config = config_manager
        self.db = db_manager
        self.executor = strategy_executor
        self.bot_token = os.getenv('TG_BOT_TOKEN')
        self.chat_id = os.getenv('TG_CHAT_ID')
        self.app = None

        if not self.bot_token:
            logger.warning("⚠️ TG_BOT_TOKEN not set - Telegram Bot disabled")
            return

        self._init_bot()

    def _init_bot(self):
        """初始化Bot"""
        try:
            self.app = Application.builder().token(self.bot_token).build()

            # 注册命令处理器
            self.app.add_handler(CommandHandler("start", self.cmd_start))
            self.app.add_handler(CommandHandler("help", self.cmd_help))
            self.app.add_handler(CommandHandler("balance", self.cmd_balance))
            self.app.add_handler(CommandHandler("positions", self.cmd_positions))
            self.app.add_handler(CommandHandler("opportunities", self.cmd_opportunities))
            self.app.add_handler(CommandHandler("status", self.cmd_status))
            self.app.add_handler(CommandHandler("pause", self.cmd_pause))
            self.app.add_handler(CommandHandler("resume", self.cmd_resume))
            self.app.add_handler(CommandHandler("close", self.cmd_close))

            logger.info("✅ Telegram Bot initialized")

        except Exception as e:
            logger.error(f"Failed to initialize Telegram Bot: {e}")

    def start(self):
        """启动Bot (异步初始化,不阻塞)"""
        if not self.app:
            return

        logger.info("Starting Telegram Bot...")
        import asyncio
        import threading

        # 在单独的线程中运行asyncio event loop
        def run_bot():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.app.initialize())
            loop.run_until_complete(self.app.start())
            loop.run_until_complete(self.app.updater.start_polling())
            # Keep the loop running
            loop.run_forever()

        self.bot_thread = threading.Thread(target=run_bot, daemon=True)
        self.bot_thread.start()
        logger.info("✅ Telegram Bot started in background thread")

    def stop(self):
        """停止Bot"""
        if self.app:
            logger.info("Stopping Telegram Bot...")
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.app.updater.stop())
            loop.run_until_complete(self.app.stop())
            loop.run_until_complete(self.app.shutdown())
            loop.close()

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start命令"""
        await update.message.reply_text(
            "🤖 资金费率套利系统\n\n"
            "欢迎使用！使用 /help 查看可用命令。"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help命令"""
        help_text = """
📊 查询命令:
/balance - 查看余额
/positions - 查看持仓
/opportunities - 当前机会
/status - 系统状态

⚙️ 控制命令:
/pause - 暂停所有策略
/resume - 恢复策略
/close <ID> - 平仓指定持仓
        """
        await update.message.reply_text(help_text)

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看余额"""
        # TODO: 实现余额查询
        await update.message.reply_text("💰 余额功能开发中...")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看持仓"""
        try:
            positions = self.executor.get_open_positions()

            if not positions:
                await update.message.reply_text("📊 当前无持仓")
                return

            summary = self.executor.get_position_summary()

            text = f"💼 当前持仓 ({summary['total_positions']}个)\n\n"
            text += f"总浮盈: {summary['total_pnl']:.2f} USDT\n"
            text += f"总资金: {summary['total_size']:.2f} USDT\n\n"

            for i, pos in enumerate(positions[:5], 1):  # 只显示前5个
                pnl = float(pos.get('current_pnl', 0))
                size = float(pos.get('position_size', 0))
                pnl_pct = (pnl / size * 100) if size > 0 else 0

                text += f"#{pos['id']} {pos['symbol']}\n"
                text += f"  {pos['strategy_type']}\n"
                text += f"  {'📈' if pnl >= 0 else '📉'} {pnl:+.2f} USDT ({pnl_pct:+.2f}%)\n\n"

            if len(positions) > 5:
                text += f"... 还有 {len(positions) - 5} 个持仓"

            await update.message.reply_text(text)

        except Exception as e:
            logger.error(f"Error in cmd_positions: {e}")
            await update.message.reply_text(f"❌ 查询失败: {str(e)}")

    async def cmd_opportunities(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看当前机会"""
        await update.message.reply_text("🔥 机会监控功能开发中...")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """系统状态"""
        try:
            summary = self.executor.get_position_summary()

            text = "📊 系统状态\n\n"
            text += f"持仓数: {summary['total_positions']}\n"
            text += f"总浮盈: {summary['total_pnl']:.2f} USDT\n"
            text += f"占用资金: {summary['total_size']:.2f} USDT\n\n"

            text += "策略分布:\n"
            for strategy, data in summary['by_strategy'].items():
                text += f"  • {strategy}: {data['count']}单\n"

            await update.message.reply_text(text)

        except Exception as e:
            logger.error(f"Error in cmd_status: {e}")
            await update.message.reply_text(f"❌ 查询失败: {str(e)}")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """暂停策略"""
        # TODO: 实现暂停功能
        await update.message.reply_text("⏸ 暂停功能开发中...")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """恢复策略"""
        # TODO: 实现恢复功能
        await update.message.reply_text("▶️ 恢复功能开发中...")

    async def cmd_close(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """平仓"""
        try:
            if not context.args:
                await update.message.reply_text("❌ 请指定持仓ID: /close <ID>")
                return

            position_id = int(context.args[0])

            if self.executor.close_position(position_id):
                await update.message.reply_text(f"✅ 持仓 #{position_id} 已平仓")
            else:
                await update.message.reply_text(f"❌ 平仓失败")

        except ValueError:
            await update.message.reply_text("❌ 无效的持仓ID")
        except Exception as e:
            logger.error(f"Error in cmd_close: {e}")
            await update.message.reply_text(f"❌ 平仓失败: {str(e)}")

    async def send_notification(self, message: str):
        """发送通知"""
        if not self.app or not self.chat_id:
            return

        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error sending notification: {e}")

    def notify_position_opened(self, data: Dict[str, Any]):
        """持仓开仓通知"""
        opportunity = data['opportunity']
        position_id = data['position_id']

        message = f"""
✅ <b>自动开仓成功</b>

策略: {opportunity['type']}
币种: {opportunity['symbol']}
开仓金额: {opportunity['position_size']:.2f} USDT
预期收益: {opportunity['expected_return']:.2f} USDT ({opportunity['expected_return_pct']*100:.2f}%)

持仓ID: #{position_id}
        """

        # 异步发送
        import asyncio
        try:
            asyncio.create_task(self.send_notification(message))
        except:
            pass

    def notify_opportunity_found(self, opportunity: Dict[str, Any]):
        """发现机会通知（需确认）"""
        message = f"""
🔔 <b>发现高收益机会</b>

策略: {opportunity['type']}
币种: {opportunity['symbol']}
预期收益: {opportunity['expected_return']:.2f} USDT ({opportunity['expected_return_pct']*100:.2f}%)
风险等级: {opportunity['risk_level']}

⚠️ 需要人工确认
        """

        import asyncio
        try:
            asyncio.create_task(self.send_notification(message))
        except:
            pass

    def notify_risk_event(self, event: Dict[str, Any]):
        """风险事件通知"""
        level_emoji = {
            'warning': '⚠️',
            'critical': '🔴',
            'emergency': '🚨'
        }

        emoji = level_emoji.get(event['level'], '⚠️')

        message = f"""
{emoji} <b>风险预警 - {event['level'].upper()}</b>

{event['description']}

时间: {event['timestamp']}
        """

        import asyncio
        try:
            asyncio.create_task(self.send_notification(message))
        except:
            pass
