"""
数据库管理器
负责数据库初始化、连接管理、基本CRUD操作
"""
import sqlite3
import os
import time
from typing import List, Dict, Any, Iterable, Sequence
from loguru import logger
from contextlib import contextmanager


class DatabaseManager:
    def __init__(self, db_path: str = "data/database.db"):
        self.db_path = db_path
        self._ensure_data_directory()

    def _ensure_data_directory(self):
        """确保数据目录存在"""
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    @contextmanager
    def get_connection(self):
        """获取数据库连接（连接阶段重试，避免 contextmanager 重复 yield）"""
        import time

        conn = None
        for attempt in range(3):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                break
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < 2:
                    logger.warning(f"Database locked, retrying {attempt + 1}/3...")
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_database(self):
        """初始化数据库表结构"""
        logger.info("Initializing database...")

        with self.get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cursor = conn.cursor()

            # 配置表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category VARCHAR(50),
                    key VARCHAR(100),
                    value TEXT,
                    is_hot_reload BOOLEAN DEFAULT TRUE,
                    description TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(category, key)
                )
            """)

            # 资金费率历史表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS funding_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange VARCHAR(20),
                    symbol VARCHAR(20),
                    timestamp BIGINT,
                    funding_rate DECIMAL(10,6),
                    next_funding_time BIGINT,
                    funding_interval BIGINT,
                    UNIQUE(exchange, symbol, timestamp)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_funding_rates
                ON funding_rates(exchange, symbol, timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_funding_rates_ts
                ON funding_rates(timestamp)
            """)

            # 稳定币价格表（脱锚监控）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stablecoin_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange VARCHAR(20),
                    symbol VARCHAR(20),
                    timestamp BIGINT,
                    price DECIMAL(18,8),
                    bid DECIMAL(18,8),
                    ask DECIMAL(18,8),
                    UNIQUE(exchange, symbol, timestamp)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_stablecoin_prices
                ON stablecoin_prices(symbol, timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_stablecoin_prices_ts
                ON stablecoin_prices(timestamp)
            """)

            # 参考价表（黄金/欧元/股票，来自 Yahoo Finance）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reference_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source VARCHAR(20),
                    symbol VARCHAR(30),
                    timestamp BIGINT,
                    price DECIMAL(18,8),
                    category VARCHAR(20),
                    UNIQUE(source, symbol, timestamp)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reference_prices
                ON reference_prices(symbol, timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_reference_prices_ts
                ON reference_prices(timestamp)
            """)

            # 告警表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_type VARCHAR(50),
                    level VARCHAR(20),
                    symbol VARCHAR(30),
                    exchange VARCHAR(40),
                    message TEXT,
                    details TEXT,
                    pushed BOOLEAN DEFAULT FALSE,
                    timestamp BIGINT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts
                ON alerts(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_alerts_dedup
                ON alerts(alert_type, symbol, exchange, timestamp)
            """)

            # 市场价格最新快照表（每所每币仅一行，基差只需最新值）
            # 旧版 market_prices 会按 30s 全量追加，8h 即膨胀到 100 万行，故迁移删除
            cursor.execute("DROP TABLE IF EXISTS market_prices")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_prices_latest (
                    exchange VARCHAR(20) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    timestamp BIGINT,
                    spot_bid DECIMAL(18,8),
                    spot_ask DECIMAL(18,8),
                    spot_price DECIMAL(18,8),
                    futures_bid DECIMAL(18,8),
                    futures_ask DECIMAL(18,8),
                    futures_price DECIMAL(18,8),
                    PRIMARY KEY (exchange, symbol)
                )
            """)

            logger.info("Database initialized successfully")

    # 数据保留策略（天）：只保留面板需要的时间窗口，防止数据库无限膨胀
    RETENTION_DAYS = {
        'market_prices_latest': 1,  # 基差最新快照；陈旧行按天清理
        'funding_rates': 1,         # 费率历史仅保留 1 天
        'reference_prices': 1,      # 仅用于最新参考价
        'stablecoin_prices': 8,     # 面板历史曲线最长 7 天
        'alerts': 30,               # 告警历史面板最长 7 天
    }

    def cleanup_old_data(self) -> int:
        """按保留策略清理历史数据，返回删除行数"""
        now_ms = int(time.time() * 1000)
        total = 0
        for table, days in self.RETENTION_DAYS.items():
            cutoff = now_ms - days * 86400 * 1000
            try:
                with self.get_connection() as conn:
                    cur = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
                    deleted = cur.rowcount
                total += max(deleted, 0)
                if deleted:
                    logger.info(f"Retention cleanup: removed {deleted} rows from {table} (>{days}d)")
            except Exception as e:
                logger.error(f"Retention cleanup failed for {table}: {e}")
        return total

    def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """执行查询并返回结果"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def execute_many(self, query: str, params_list: Iterable[Sequence]) -> None:
        """批量写入（单连接单事务，避免逐行建连的性能损耗）"""
        rows = list(params_list)
        if not rows:
            return
        with self.get_connection() as conn:
            conn.executemany(query, rows)

    def set_config(self, category: str, key: str, value: str,
                   is_hot_reload: bool = True, description: str = ""):
        """设置配置值"""
        self.execute_query(
            """
            INSERT INTO config (category, key, value, is_hot_reload, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(category, key) DO UPDATE SET
                value = excluded.value,
                is_hot_reload = excluded.is_hot_reload,
                description = excluded.description,
                updated_at = CURRENT_TIMESTAMP
            """,
            (category, key, value, is_hot_reload, description)
        )
