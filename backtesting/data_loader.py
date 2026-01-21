"""
回测数据加载器
负责加载和准备历史数据用于回测
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from loguru import logger
import pandas as pd


class DataLoader:
    """数据加载器"""

    def __init__(self, db_manager):
        """初始化数据加载器"""
        self.db_manager = db_manager

    def load_funding_rates(
        self,
        start_date: str,
        end_date: str,
        exchanges: Optional[List[str]] = None,
        symbols: Optional[List[str]] = None
    ) -> pd.DataFrame:
        """
        加载资金费率数据

        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            exchanges: 交易所列表，None表示全部
            symbols: 交易对列表，None表示全部

        Returns:
            DataFrame包含资金费率数据
        """
        try:
            query = """
                SELECT exchange, symbol, funding_rate, timestamp
                FROM funding_rates
                WHERE timestamp >= ? AND timestamp <= ?
            """
            params = [start_date, end_date]

            if exchanges:
                placeholders = ','.join(['?' for _ in exchanges])
                query += f" AND exchange IN ({placeholders})"
                params.extend(exchanges)

            if symbols:
                placeholders = ','.join(['?' for _ in symbols])
                query += f" AND symbol IN ({placeholders})"
                params.extend(symbols)

            query += " ORDER BY timestamp ASC"

            with self.db_manager.get_connection() as conn:
                df = pd.read_sql_query(query, conn, params=params)

            logger.info(f"Loaded {len(df)} funding rate records")
            return df

        except Exception as e:
            logger.error(f"Error loading funding rates: {e}")
            return pd.DataFrame()

    def get_available_date_range(self) -> Dict[str, str]:
        """获取可用的数据日期范围"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT MIN(timestamp) as min_date, MAX(timestamp) as max_date
                    FROM funding_rates
                """)
                result = cursor.fetchone()

                if result and result[0]:
                    return {
                        'start_date': result[0],
                        'end_date': result[1]
                    }
                return {'start_date': None, 'end_date': None}

        except Exception as e:
            logger.error(f"Error getting date range: {e}")
            return {'start_date': None, 'end_date': None}

    def get_available_symbols(self) -> List[str]:
        """获取所有可用的交易对"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT symbol FROM funding_rates ORDER BY symbol")
                return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting symbols: {e}")
            return []

    def get_available_exchanges(self) -> List[str]:
        """获取所有可用的交易所"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT exchange FROM funding_rates ORDER BY exchange")
                return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error getting exchanges: {e}")
            return []

    def calculate_statistics(self, df: pd.DataFrame) -> Dict[str, Any]:
        """计算数据统计信息"""
        if df.empty:
            return {}

        stats = {
            'total_records': len(df),
            'date_range': {
                'start': df['timestamp'].min(),
                'end': df['timestamp'].max()
            },
            'exchanges': df['exchange'].unique().tolist(),
            'symbols': df['symbol'].unique().tolist(),
            'funding_rate_stats': {
                'mean': df['funding_rate'].mean(),
                'median': df['funding_rate'].median(),
                'std': df['funding_rate'].std(),
                'min': df['funding_rate'].min(),
                'max': df['funding_rate'].max()
            }
        }

        return stats
