"""
数据库管理器
负责数据库初始化、连接管理、基本CRUD操作
"""
import sqlite3
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
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

            # 交易所账户表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exchange_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange_name VARCHAR(20) UNIQUE,
                    api_key TEXT,
                    api_secret TEXT,
                    passphrase TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 交易对配置表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trading_pair_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol VARCHAR(20),
                    exchange VARCHAR(20),

                    strategy1_enabled BOOLEAN DEFAULT TRUE,
                    strategy2a_enabled BOOLEAN DEFAULT TRUE,
                    strategy2b_enabled BOOLEAN DEFAULT TRUE,

                    s1_execution_mode VARCHAR(10) DEFAULT 'auto',
                    s1_min_funding_diff DECIMAL(10,6),
                    s1_position_size DECIMAL(18,2),
                    s1_target_exchanges TEXT,

                    s2a_execution_mode VARCHAR(10) DEFAULT 'auto',
                    s2a_min_funding_rate DECIMAL(10,6),
                    s2a_position_size DECIMAL(18,2),
                    s2a_max_basis_deviation DECIMAL(10,6),

                    s2b_execution_mode VARCHAR(10) DEFAULT 'manual',
                    s2b_min_basis DECIMAL(10,6),
                    s2b_position_size DECIMAL(18,2),
                    s2b_target_return DECIMAL(10,6),

                    strategy3_enabled BOOLEAN DEFAULT FALSE,
                    s3_min_funding_rate DECIMAL(10,6),
                    s3_position_pct DECIMAL(10,4),
                    s3_stop_loss_pct DECIMAL(10,4),
                    s3_check_basis BOOLEAN DEFAULT TRUE,
                    s3_short_exit_threshold DECIMAL(10,6),
                    s3_long_exit_threshold DECIMAL(10,6),
                    s3_trailing_stop_enabled BOOLEAN DEFAULT TRUE,
                    s3_trailing_activation_pct DECIMAL(10,4) DEFAULT 0.04,
                    s3_trailing_callback_pct DECIMAL(10,4) DEFAULT 0.04,

                    max_positions INTEGER DEFAULT 3,
                    priority INTEGER DEFAULT 5,
                    is_active BOOLEAN DEFAULT TRUE,
                    notes TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, exchange)
                )
            """)

            # K线表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS klines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange VARCHAR(20),
                    symbol VARCHAR(20),
                    timeframe VARCHAR(10),
                    timestamp BIGINT,
                    open DECIMAL(18,8),
                    high DECIMAL(18,8),
                    low DECIMAL(18,8),
                    close DECIMAL(18,8),
                    volume DECIMAL(18,8),
                    UNIQUE(exchange, symbol, timeframe, timestamp)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_klines
                ON klines(exchange, symbol, timestamp)
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

            # 订单记录表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id INTEGER,
                    strategy_type VARCHAR(50),
                    exchange VARCHAR(20),
                    symbol VARCHAR(20),
                    side VARCHAR(10),
                    order_type VARCHAR(10),
                    price DECIMAL(18,8),
                    amount DECIMAL(18,8),
                    filled DECIMAL(18,8),
                    status VARCHAR(20),
                    order_id VARCHAR(100),
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_orders
                ON orders(strategy_type, create_time)
            """)

            # 持仓表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_type VARCHAR(50),
                    symbol VARCHAR(20),
                    exchanges TEXT,
                    entry_details TEXT,
                    position_size DECIMAL(18,2),
                    current_pnl DECIMAL(18,2),
                    realized_pnl DECIMAL(18,2),
                    funding_collected DECIMAL(18,2),
                    fees_paid DECIMAL(18,2),
                    status VARCHAR(20),
                    open_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    close_time TIMESTAMP,
                    trailing_stop_activated BOOLEAN DEFAULT FALSE,
                    best_price DECIMAL(20,8) DEFAULT NULL,
                    activation_price DECIMAL(20,8) DEFAULT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_positions
                ON positions(status, open_time)
            """)

            # 策略日志表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_type VARCHAR(50),
                    action VARCHAR(50),
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 风险事件表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level VARCHAR(20),
                    event_type VARCHAR(50),
                    description TEXT,
                    position_id INTEGER,
                    is_handled BOOLEAN DEFAULT FALSE,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 回测结果表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backtest_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100),
                    strategy_type VARCHAR(50),
                    strategy_params TEXT,
                    start_date DATE,
                    end_date DATE,
                    initial_capital DECIMAL(18,2),
                    final_capital DECIMAL(18,2),
                    total_return DECIMAL(10,4),
                    annual_return DECIMAL(10,4),
                    sharpe_ratio DECIMAL(10,4),
                    max_drawdown DECIMAL(10,4),
                    win_rate DECIMAL(10,4),
                    total_trades INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 迁移：为 positions 表添加 trailing stop 字段
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN trailing_stop_activated BOOLEAN DEFAULT FALSE")
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN best_price DECIMAL(20,8) DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE positions ADD COLUMN activation_price DECIMAL(20,8) DEFAULT NULL")
            except sqlite3.OperationalError:
                pass

            # 迁移：为 trading_pair_configs 表添加 trailing stop 配置字段
            try:
                cursor.execute("ALTER TABLE trading_pair_configs ADD COLUMN s3_trailing_stop_enabled BOOLEAN DEFAULT TRUE")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE trading_pair_configs ADD COLUMN s3_trailing_activation_pct DECIMAL(10,4) DEFAULT 0.04")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE trading_pair_configs ADD COLUMN s3_trailing_callback_pct DECIMAL(10,4) DEFAULT 0.04")
            except sqlite3.OperationalError:
                pass

            conn.commit()
            logger.info("Database initialized successfully")

    def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """执行查询并返回结果"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def execute_update(self, query: str, params: tuple = ()) -> int:
        """执行更新/插入/删除操作，返回影响的行数"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.rowcount

    def execute_insert(self, query: str, params: tuple = ()) -> int:
        """执行插入操作，返回新插入的行ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.lastrowid

    def get_config(self, category: str, key: str) -> Optional[str]:
        """获取配置值"""
        result = self.execute_query(
            "SELECT value FROM config WHERE category = ? AND key = ?",
            (category, key)
        )
        return result[0]['value'] if result else None

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

    def backup_database(self, backup_path: Optional[str] = None):
        """备份数据库"""
        if backup_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"data/backups/database_backup_{timestamp}.db"

        os.makedirs(os.path.dirname(backup_path), exist_ok=True)

        with self.get_connection() as conn:
            backup_conn = sqlite3.connect(backup_path)
            conn.backup(backup_conn)
            backup_conn.close()

        logger.info(f"Database backed up to {backup_path}")
        return backup_path
