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

        # TODO: 初始化其他模块
        # self.data_collector = DataCollector(...)
        # self.opportunity_monitor = OpportunityMonitor(...)
        # self.strategy_executor = StrategyExecutor(...)
        # self.risk_manager = RiskManager(...)
        # self.web_app = create_app(...)
        # self.tg_bot = TelegramBot(...)

        logger.info("System initialization completed")
        logger.info("=" * 60)

    def start(self):
        """启动所有组件"""
        logger.info("Starting Funding Rate Arbitrage System...")

        try:
            # TODO: 启动各个模块
            # 启动数据采集器
            # threading.Thread(target=self.data_collector.start, daemon=True).start()

            # 启动机会监控器
            # threading.Thread(target=self.opportunity_monitor.start, daemon=True).start()

            # 启动策略执行器
            # threading.Thread(target=self.strategy_executor.start, daemon=True).start()

            # 启动TG Bot
            # threading.Thread(target=self.tg_bot.start, daemon=True).start()

            # 启动Web服务（主线程）
            logger.info("=" * 60)
            logger.info("Web server will start at http://0.0.0.0:5000")
            logger.info("Press Ctrl+C to stop the system")
            logger.info("=" * 60)

            # self.web_app.run(host='0.0.0.0', port=5000, debug=False)

            # 临时：保持主线程运行
            logger.info("System is running... (Press Ctrl+C to stop)")
            while True:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            self.stop()

    def stop(self):
        """停止所有组件"""
        logger.info("Stopping Funding Rate Arbitrage System...")

        # TODO: 停止各个模块
        # self.data_collector.stop()
        # self.opportunity_monitor.stop()
        # self.strategy_executor.stop()
        # self.tg_bot.stop()

        logger.info("System stopped successfully")
        logger.info("=" * 60)


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
