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

    def __init__(self, config_manager, db_manager, strategy_executor, opportunity_monitor=None):
        self.config = config_manager
        self.db = db_manager
        self.executor = strategy_executor
        self.opportunity_monitor = opportunity_monitor
        self.bot_token = os.getenv('TG_BOT_TOKEN')
        self.chat_id = os.getenv('TG_CHAT_ID')
        self.app = None
        self.paused = False  # 暂停状态

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
            self.app.add_handler(CommandHandler("report", self.cmd_report))
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
/report - 查看今日报告

⚙️ 控制命令:
/pause - 暂停所有策略
/resume - 恢复策略
/close <ID> - 平仓指定持仓
        """
        await update.message.reply_text(help_text)

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看余额"""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # 获取持仓统计
                cursor.execute("""
                    SELECT
                        COUNT(*) as total_positions,
                        SUM(CASE WHEN status='open' THEN position_size ELSE 0 END) as occupied_capital,
                        SUM(CASE WHEN status='open' THEN current_pnl ELSE 0 END) as unrealized_pnl,
                        SUM(CASE WHEN status='closed' THEN realized_pnl ELSE 0 END) as realized_pnl
                    FROM positions
                """)
                stats = cursor.fetchone()

                total_capital = self.config.get('global', 'total_capital', 100000)
                occupied = stats[1] or 0
                unrealized = stats[2] or 0
                realized = stats[3] or 0
                available = total_capital - occupied

                text = "💰 <b>资金概览</b>\n\n"
                text += f"总资金: {total_capital:.2f} USDT\n"
                text += f"可用资金: {available:.2f} USDT\n"
                text += f"占用资金: {occupied:.2f} USDT\n\n"
                text += f"未实现盈亏: {unrealized:+.2f} USDT\n"
                text += f"已实现盈亏: {realized:+.2f} USDT\n"
                text += f"总盈亏: {(unrealized + realized):+.2f} USDT\n"

                await update.message.reply_text(text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in cmd_balance: {e}")
            await update.message.reply_text(f"❌ 查询失败: {str(e)}")

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
        try:
            if not self.opportunity_monitor:
                await update.message.reply_text("❌ 机会监控器未初始化")
                return

            opportunities = self.opportunity_monitor.get_opportunities(limit=5)

            if not opportunities:
                await update.message.reply_text("📊 当前无高收益机会")
                return

            text = "🎯 <b>当前套利机会</b>\n\n"

            for i, opp in enumerate(opportunities[:5], 1):
                symbol = opp.get('symbol', 'N/A')
                strategy = opp.get('type', 'N/A')
                expected_return = opp.get('expected_return', 0)
                expected_pct = opp.get('expected_return_pct', 0) * 100

                text += f"{i}. {symbol} ({strategy})\n"
                text += f"   预期收益: {expected_return:.2f} USDT ({expected_pct:.2f}%)\n\n"

            await update.message.reply_text(text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in cmd_opportunities: {e}")
            await update.message.reply_text(f"❌ 查询失败: {str(e)}")

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

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看今日报告"""
        try:
            from datetime import datetime, timedelta

            today = datetime.now().date()
            today_str = today.isoformat()

            with self.db.get_connection() as conn:
                cursor = conn.cursor()

                # 今日交易统计
                cursor.execute("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed,
                        SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_today,
                        SUM(CASE WHEN status='closed' THEN realized_pnl ELSE 0 END) as realized_pnl,
                        SUM(CASE WHEN status='closed' THEN fees_paid ELSE 0 END) as fees
                    FROM positions
                    WHERE DATE(open_time) = ?
                """, (today_str,))

                stats = cursor.fetchone()

                text = f"📊 <b>今日报告 - {today_str}</b>\n\n"
                text += "📈 <b>交易统计:</b>\n"
                text += f"  今日开仓: {stats[0]} 单\n"
                text += f"  已平仓: {stats[1]} 单\n"
                text += f"  持仓中: {stats[2]} 单\n\n"

                text += "💰 <b>盈亏统计:</b>\n"
                realized = stats[3] or 0
                fees = stats[4] or 0
                net = realized - fees

                text += f"  已实现盈亏: {realized:+.2f} USDT\n"
                text += f"  手续费: {fees:.2f} USDT\n"
                text += f"  净盈亏: {net:+.2f} USDT\n"

                await update.message.reply_text(text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error in cmd_report: {e}")
            await update.message.reply_text(f"❌ 查询失败: {str(e)}")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """暂停策略"""
        try:
            self.paused = True
            # 通知策略执行器暂停
            if hasattr(self.executor, 'set_paused'):
                self.executor.set_paused(True)

            await update.message.reply_text("⏸ <b>所有策略已暂停</b>\n\n不会开新仓，现有持仓继续持有\n使用 /resume 恢复", parse_mode='HTML')
            logger.info("Strategies paused via Telegram command")

        except Exception as e:
            logger.error(f"Error in cmd_pause: {e}")
            await update.message.reply_text(f"❌ 暂停失败: {str(e)}")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """恢复策略"""
        try:
            self.paused = False
            # 通知策略执行器恢复
            if hasattr(self.executor, 'set_paused'):
                self.executor.set_paused(False)

            await update.message.reply_text("▶️ <b>策略已恢复</b>\n\n系统将继续监控并执行套利机会", parse_mode='HTML')
            logger.info("Strategies resumed via Telegram command")

        except Exception as e:
            logger.error(f"Error in cmd_resume: {e}")
            await update.message.reply_text(f"❌ 恢复失败: {str(e)}")

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

    def send_daily_report(self, report: Dict[str, Any]):
        """发送每日报告"""
        message = f"""
📊 <b>每日报告 - {report['date']}</b>

💰 盈亏统计:
• 总盈亏: {report['total_pnl']:.2f} USDT
• 总手续费: {report['total_fees']:.2f} USDT
• 净盈亏: {report['net_pnl']:.2f} USDT

📈 持仓统计:
• 今日开仓: {report['total_positions']} 单
• 当前持仓: {report['open_positions']} 单
• 已平仓: {report['closed_positions']} 单

生成时间: {report.get('generated_at', 'N/A')}
        """

        import asyncio
        try:
            asyncio.create_task(self.send_notification(message))
        except:
            pass
