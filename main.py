"""
资金费率监控系统 - 主程序入口
纯监控模式：采集市场数据 -> 异常检测 -> 告警推送 -> Web 面板
"""
import os
import sys
import threading
import time
from dotenv import load_dotenv
from utils.logger import setup_logger
from database import DatabaseManager
from config import ConfigManager

load_dotenv()

logger = setup_logger(
    log_file=os.getenv('LOG_FILE', 'logs/app.log'),
    log_level=os.getenv('LOG_LEVEL', 'INFO')
)


class FundingRateMonitorSystem:
    """资金费率监控系统主类"""

    def __init__(self):
        logger.info("=" * 60)
        logger.info("Initializing Funding Rate Monitor System...")
        logger.info("=" * 60)

        # 数据库
        self.db_path = os.getenv('DATABASE_PATH', 'data/database.db')
        self.db_manager = DatabaseManager(self.db_path)
        self.db_manager.init_database()
        self.db_manager.cleanup_old_data()

        # 配置
        self.config_manager = ConfigManager(self.db_manager)
        self.config_manager.init_default_configs()

        # 核心模块
        from core import DataCollector, MarketMonitor, AlertManager
        from bot import TelegramBot

        self.data_collector = DataCollector(self.config_manager, self.db_manager)
        self.market_monitor = MarketMonitor(self.config_manager, self.db_manager, self.data_collector)
        self.alert_manager = AlertManager(self.config_manager, self.db_manager)
        self.tg_bot = TelegramBot(self.config_manager, self.db_manager,
                                  self.market_monitor, self.alert_manager)

        # 告警链路：monitor -> alert_manager -> telegram
        self.market_monitor.register_alert_callback(self.alert_manager.handle_alerts)
        self.alert_manager.register_push_callback(self.tg_bot.push_alert)

        logger.info("System initialization completed")
        logger.info("=" * 60)

    def start(self):
        """启动所有组件"""
        logger.info("Starting Funding Rate Monitor System...")

        try:
            self.data_collector.start()
            self.market_monitor.start()
            self.alert_manager.start()

            # TG Bot（后台线程）
            if self.tg_bot.app:
                self.tg_bot.start()

            # 每日汇总 + 数据保留清理任务
            threading.Thread(target=self._daily_summary_loop, daemon=True).start()
            threading.Thread(target=self._cleanup_loop, daemon=True).start()

            # Web 服务
            from web.app import create_app, run_web_server
            web_app = create_app(
                self.config_manager,
                self.db_manager,
                self.data_collector,
                self.market_monitor,
                self.alert_manager
            )

            web_host = os.getenv('WEB_HOST', '0.0.0.0')
            web_port = int(os.getenv('WEB_PORT', 5000))

            logger.info("=" * 60)
            logger.info("System is running (monitor-only mode)...")
            logger.info(f"Web UI: http://{web_host}:{web_port}")
            logger.info("Press Ctrl+C to stop")
            logger.info("=" * 60)

            run_web_server(web_app, host=web_host, port=web_port)

        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            self.stop()

    def stop(self):
        logger.info("Stopping system...")
        self.data_collector.stop()
        self.market_monitor.stop()
        self.alert_manager.stop()
        self.tg_bot.stop()
        logger.info("System stopped")

    def _daily_summary_loop(self):
        """每日汇总推送（按配置小时）"""
        last_sent_date = None
        while True:
            try:
                if self.config_manager.get_bool('alerts', 'daily_summary_enabled', True):
                    from datetime import datetime
                    now = datetime.now()
                    target_hour = self.config_manager.get_int('alerts', 'daily_summary_hour', 9)
                    today = now.date()
                    if now.hour == target_hour and last_sent_date != today:
                        scan = self.market_monitor.last_scan
                        if not scan:
                            logger.warning("Daily summary skipped: no scan yet")
                        else:
                            ranking = [r for r in scan.get('apr_ranking', []) if r.get('apr') is not None]
                            summary = {
                                'top_apr': sorted(ranking, key=lambda r: abs(r['apr']), reverse=True)[:5],
                                'alert_stats': self.alert_manager.get_alert_stats(24),
                            }
                            if self.tg_bot.app:
                                self.tg_bot.push_daily_summary(summary)
                            logger.info("Daily summary pushed")
                        last_sent_date = today
            except Exception as e:
                logger.error(f"Daily summary error: {e}")
            time.sleep(60)

    def _cleanup_loop(self):
        """每 6 小时按保留策略清理历史数据"""
        while True:
            time.sleep(6 * 3600)
            try:
                self.db_manager.cleanup_old_data()
            except Exception as e:
                logger.error(f"Retention cleanup error: {e}")


def main():
    print("""
    ============================================================
      Funding Rate Monitor System v2.0.0
      Market Monitor + Alert Push (No Trading)
    ============================================================
    """)

    try:
        system = FundingRateMonitorSystem()
        system.start()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
