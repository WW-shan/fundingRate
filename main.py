"""
资金费率套利系统 - 主程序入口
"""
import os
import sys
import threading
import time
from dotenv import load_dotenv
from utils.logger import setup_logger
from database import DatabaseManager
from config import ConfigManager

# 加载环境变量
load_dotenv()

# 初始化日志
logger = setup_logger(
    log_file=os.getenv('LOG_FILE', 'logs/app.log'),
    log_level=os.getenv('LOG_LEVEL', 'INFO')
)


class FundingRateArbitrageSystem:
    """资金费率套利系统主类"""

    def __init__(self):
        logger.info("=" * 60)
        logger.info("Initializing Funding Rate Arbitrage System...")
        logger.info("=" * 60)

        # 初始化数据库
        self.db_path = os.getenv('DATABASE_PATH', 'data/database.db')
        self.db_manager = DatabaseManager(self.db_path)
        self.db_manager.init_database()

        # 初始化配置管理器
        self.config_manager = ConfigManager(self.db_manager)
        self.config_manager.init_default_configs()

        # 检查是否启用实际交易
        self.enable_trading = os.getenv('ENABLE_TRADING', 'False').lower() == 'true'
        if not self.enable_trading:
            logger.warning("⚠️  TRADING IS DISABLED - Running in simulation mode")
            logger.warning("⚠️  Set ENABLE_TRADING=True in .env to enable real trading")
        else:
            logger.info("✅ Trading is ENABLED")

        # 初始化核心模块
        from core import DataCollector, OpportunityMonitor, RiskManager, OrderManager, StrategyExecutor
        from bot import TelegramBot

        self.data_collector = DataCollector(self.config_manager, self.db_manager)
        self.risk_manager = RiskManager(self.config_manager, self.db_manager)
        self.opportunity_monitor = OpportunityMonitor(self.config_manager, self.db_manager, self.data_collector)
        self.order_manager = OrderManager(self.db_manager, self.data_collector.exchanges)
        self.strategy_executor = StrategyExecutor(self.config_manager, self.db_manager, self.risk_manager, self.order_manager)
        self.tg_bot = TelegramBot(self.config_manager, self.db_manager, self.strategy_executor)

        # 注册回调
        self.opportunity_monitor.register_callback(self._on_opportunities_found)
        self.risk_manager.register_callback(self._on_risk_event)
        self.strategy_executor.register_callback(self._on_execution_event)

        logger.info("System initialization completed")
        logger.info("=" * 60)

    def start(self):
        """启动所有组件"""
        logger.info("Starting Funding Rate Arbitrage System...")

        try:
            # 启动数据采集器
            self.data_collector.start()

            # 启动机会监控器
            self.opportunity_monitor.start()

            # 启动风险管理器
            self.risk_manager.start()

            # 启动策略执行器
            self.strategy_executor.start()

            # 启动TG Bot（在后台线程）
            if self.tg_bot.app:
                threading.Thread(target=self.tg_bot.start, daemon=True).start()

            # 启动定时任务（备份和报告）
            threading.Thread(target=self._daily_tasks, daemon=True).start()

            # 启动Web服务
            from web.app import create_app, run_web_server
            web_app = create_app(
                self.config_manager,
                self.db_manager,
                self.data_collector,
                self.opportunity_monitor,
                self.strategy_executor,
                self.risk_manager
            )

            web_host = os.getenv('WEB_HOST', '0.0.0.0')
            web_port = int(os.getenv('WEB_PORT', 5000))

            logger.info("=" * 60)
            logger.info("System is running...")
            logger.info(f"Web UI: http://{web_host}:{web_port}")
            logger.info("Press Ctrl+C to stop the system")
            logger.info("=" * 60)

            # 在主线程运行Web服务
            run_web_server(web_app, host=web_host, port=web_port)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            self.stop()

    def stop(self):
        """停止所有组件"""
        logger.info("Stopping Funding Rate Arbitrage System...")

        self.data_collector.stop()
        self.opportunity_monitor.stop()
        self.risk_manager.stop()
        self.strategy_executor.stop()
        self.tg_bot.stop()

        logger.info("System stopped successfully")
        logger.info("=" * 60)

    def _on_opportunities_found(self, opportunities):
        """机会发现回调"""
        # 自动提交高评分的低风险机会
        for opp in opportunities:
            if opp['risk_level'] == 'low' and opp['score'] > 70:
                self.strategy_executor.submit_opportunity(opp)

    def _on_risk_event(self, event):
        """风险事件回调"""
        # 发送到TG Bot
        self.tg_bot.notify_risk_event(event)

    def _on_execution_event(self, event_type, data):
        """执行事件回调"""
        if event_type == 'position_opened':
            self.tg_bot.notify_position_opened(data)
        elif event_type == 'opportunity_found':
            self.tg_bot.notify_opportunity_found(data)

    def _daily_tasks(self):
        """每日定时任务:备份数据库和发送报告"""
        import schedule

        def backup_job():
            """备份数据库"""
            try:
                logger.info("Starting daily database backup...")
                self.db_manager.backup_database()
                logger.info("✅ Database backup completed")
            except Exception as e:
                logger.error(f"❌ Database backup failed: {e}")

        def daily_report_job():
            """发送每日报告"""
            try:
                logger.info("Generating daily report...")
                # 生成报告数据
                report = self._generate_daily_report()
                # 通过TG Bot发送
                if self.tg_bot.app:
                    self.tg_bot.send_daily_report(report)
                logger.info("✅ Daily report sent")
            except Exception as e:
                logger.error(f"❌ Daily report failed: {e}")

        # 每天凌晨2点备份
        schedule.every().day.at("02:00").do(backup_job)
        # 每天早上9点发送报告
        schedule.every().day.at("09:00").do(daily_report_job)

        logger.info("Daily tasks scheduler started")

        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次

    def _generate_daily_report(self):
        """生成每日报告"""
        from datetime import datetime, timedelta

        today = datetime.now().date()
        yesterday = today - timedelta(days=1)

        with self.db_manager.get_connection() as conn:
            cursor = conn.cursor()

            # 获取今日持仓数
            cursor.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_positions,
                       SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) as closed_positions
                FROM positions
                WHERE DATE(open_time) = ?
            """, (today,))
            position_stats = cursor.fetchone()

            # 获取今日盈亏
            cursor.execute("""
                SELECT SUM(realized_pnl) as total_pnl,
                       SUM(fees_paid) as total_fees
                FROM positions
                WHERE DATE(close_time) = ? AND status='closed'
            """, (today,))
            pnl_stats = cursor.fetchone()

            report = {
                'date': str(today),
                'total_positions': position_stats['total'] or 0,
                'open_positions': position_stats['open_positions'] or 0,
                'closed_positions': position_stats['closed_positions'] or 0,
                'total_pnl': pnl_stats['total_pnl'] or 0,
                'total_fees': pnl_stats['total_fees'] or 0,
                'net_pnl': (pnl_stats['total_pnl'] or 0) - (pnl_stats['total_fees'] or 0)
            }

        return report


def main():
    """主函数"""
    # 显示欢迎信息
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║                                                           ║
    ║       Funding Rate Arbitrage System v1.0.0                ║
    ║                                                           ║
    ║       Professional Crypto Arbitrage Trading Bot          ║
    ║                                                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    # 检查必要的环境变量
    required_env_vars = []
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please check your .env file")
        sys.exit(1)

    # 创建并启动系统
    try:
        system = FundingRateArbitrageSystem()
        system.start()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
