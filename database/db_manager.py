"""
数据库管理器
负责数据库初始化、连接管理、基本CRUD操作
"""
import sqlite3
import os
from typing import List, Dict, Any
from loguru import logger
from contextlib import contextmanager


class DatabaseManager:
    def __init__(self, db_path: str = "data/database.db"):
        self.db_path = db_path
        self._ensure_data_directory()

    def _ensure_data_directory(self):
        """确保数据目录存在"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器"""
        conn = None
        max_retries = 3
        retry_delay = 0.1
        import time

        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_path, timeout=5.0)  # 增加超时时间
                conn.row_factory = sqlite3.Row  # 使结果可以通过列名访问
                yield conn
                conn.commit()
                break  # 成功执行后跳出重试循环
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    logger.warning(f"Database locked, retrying {attempt + 1}/{max_retries}...")
                    if conn:
                        try:
                            conn.rollback()
                        except:
                            pass
                        try:
                            conn.close()
                        except:
                            pass
                    time.sleep(retry_delay * (attempt + 1))  # 线性退避
                    continue
                else:
                    if conn:
                        conn.rollback()
                    logger.error(f"Database operational error: {e}")
                    raise
            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"Database error: {e}")
                raise
            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass

    def init_database(self):
        """初始化数据库表结构"""
        logger.info("Initializing database...")

        with self.get_connection() as conn:
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

            # 市场价格数据表（新增）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange VARCHAR(20),
                    symbol VARCHAR(20),
                    timestamp BIGINT,
                    spot_bid DECIMAL(18,8),
                    spot_ask DECIMAL(18,8),
                    spot_price DECIMAL(18,8),
                    futures_bid DECIMAL(18,8),
                    futures_ask DECIMAL(18,8),
                    futures_price DECIMAL(18,8),
                    maker_fee DECIMAL(10,6),
                    taker_fee DECIMAL(10,6),
                    UNIQUE(exchange, symbol, timestamp)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_market_prices
                ON market_prices(exchange, symbol, timestamp)
            """)

            conn.commit()
            logger.info("Database initialized successfully")

    def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """执行查询并返回结果"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

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
