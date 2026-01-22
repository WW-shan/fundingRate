"""
数据采集器
实时采集交易所数据：价格、资金费率、订单簿深度等
"""
import time
import threading
from typing import Dict, List, Any
from datetime import datetime
from loguru import logger
from config import ConfigManager, ExchangeAccountManager
from database import DatabaseManager
from exchanges import (
    BinanceAdapter, OKXAdapter, BybitAdapter,
    GateAdapter, BitgetAdapter
)


class DataCollector:
    """数据采集器"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager,
                 account_manager: ExchangeAccountManager):
        self.config = config_manager
        self.db = db_manager
        self.account_manager = account_manager
        self.running = False
        self.exchanges = {}
        self.market_data = {}  # 存储最新的市场数据
        self._init_exchanges()

    def _init_exchanges(self):
        """初始化所有交易所连接"""
        logger.info("Initializing exchange connections...")

        # 从账户管理器获取所有激活的账户
        exchange_accounts = self.account_manager.get_all_accounts()

        for exchange_name, cfg in exchange_accounts.items():
            exchange_name = exchange_name.lower()
            try:
                if exchange_name == 'binance':
                    self.exchanges['binance'] = BinanceAdapter(
                        cfg['api_key'], cfg['api_secret']
                    )
                elif exchange_name == 'okx':
                    self.exchanges['okx'] = OKXAdapter(
                        cfg['api_key'], cfg['api_secret'], cfg['passphrase']
                    )
                elif exchange_name == 'bybit':
                    self.exchanges['bybit'] = BybitAdapter(
                        cfg['api_key'], cfg['api_secret']
                    )
                elif exchange_name == 'gate':
                    self.exchanges['gate'] = GateAdapter(
                        cfg['api_key'], cfg['api_secret']
                    )
                elif exchange_name == 'bitget':
                    self.exchanges['bitget'] = BitgetAdapter(
                        cfg['api_key'], cfg['api_secret'], cfg['passphrase']
                    )

                # 测试连接
                if self.exchanges[exchange_name].test_connection():
                    logger.info(f"✅ {exchange_name.capitalize()} connected successfully")
                else:
                    logger.warning(f"⚠️ {exchange_name.capitalize()} connection test failed")
                    del self.exchanges[exchange_name]

            except Exception as e:
                logger.error(f"Failed to initialize {exchange_name}: {e}")

        logger.info(f"Connected to {len(self.exchanges)} exchanges: {list(self.exchanges.keys())}")

    def reload_exchanges(self):
        """重新加载交易所连接（支持热更新）"""
        logger.info("Reloading exchange connections...")
        
        # 重新加载账户信息
        self.account_manager.reload_accounts()
        
        # 关闭旧连接
        self.exchanges.clear()
        
        # 重新初始化
        self._init_exchanges()
        
        logger.info("Exchange connections reloaded")

    def start(self):
        """启动数据采集"""
        logger.info("Starting data collector...")
        self.running = True

        # 启动价格采集线程
        threading.Thread(target=self._price_collection_loop, daemon=True).start()

        # 启动资金费率采集线程
        threading.Thread(target=self._funding_rate_collection_loop, daemon=True).start()

        logger.info("Data collector started")

    def stop(self):
        """停止数据采集"""
        logger.info("Stopping data collector...")
        self.running = False

    def _price_collection_loop(self):
        """价格采集循环"""
        interval = self.config.get('global', 'price_refresh_interval', 5)

        while self.running:
            try:
                self._collect_prices()
                time.sleep(interval)
            except Exception as e:
                logger.error(f"Error in price collection loop: {e}")
                time.sleep(interval)

    def _funding_rate_collection_loop(self):
        """资金费率采集循环"""
        interval = self.config.get('global', 'funding_refresh_interval', 300)

        while self.running:
            try:
                self._collect_funding_rates()
                time.sleep(interval)
            except Exception as e:
                logger.error(f"Error in funding rate collection loop: {e}")
                time.sleep(interval)

    def _collect_prices(self):
        """采集所有交易对的价格数据"""
        # 获取需要监控的交易对
        symbols = self._get_monitored_symbols()

        for symbol in symbols:
            for exchange_name, exchange in self.exchanges.items():
                try:
                    # 初始化symbol数据结构
                    if symbol not in self.market_data:
                        self.market_data[symbol] = {}
                    if exchange_name not in self.market_data[symbol]:
                        self.market_data[symbol][exchange_name] = {}

                    # 获取现货行情
                    spot_ticker = exchange.get_spot_ticker(symbol)
                    if spot_ticker:
                        self.market_data[symbol][exchange_name]['spot_bid'] = spot_ticker.get('bid')
                        self.market_data[symbol][exchange_name]['spot_ask'] = spot_ticker.get('ask')
                        self.market_data[symbol][exchange_name]['spot_price'] = spot_ticker.get('last')

                    # 获取期货行情
                    futures_ticker = exchange.get_futures_ticker(symbol)
                    if futures_ticker:
                        self.market_data[symbol][exchange_name]['futures_bid'] = futures_ticker.get('bid')
                        self.market_data[symbol][exchange_name]['futures_ask'] = futures_ticker.get('ask')
                        self.market_data[symbol][exchange_name]['futures_price'] = futures_ticker.get('last')

                    # 获取订单簿深度
                    spot_orderbook = exchange.get_order_book(symbol, is_futures=False, limit=5)
                    self.market_data[symbol][exchange_name]['spot_depth_5'] = spot_orderbook.get('bid_depth', 0)

                    futures_orderbook = exchange.get_order_book(symbol, is_futures=True, limit=5)
                    self.market_data[symbol][exchange_name]['futures_depth_5'] = futures_orderbook.get('bid_depth', 0)

                    # 获取交易手续费
                    fees = exchange.get_trading_fees(symbol)
                    self.market_data[symbol][exchange_name]['maker_fee'] = fees.get('maker', 0.001)
                    self.market_data[symbol][exchange_name]['taker_fee'] = fees.get('taker', 0.001)

                    # 添加时间戳
                    self.market_data[symbol][exchange_name]['timestamp'] = int(time.time() * 1000)

                except Exception as e:
                    logger.debug(f"Error collecting price for {symbol} on {exchange_name}: {e}")

        logger.debug(f"Collected prices for {len(symbols)} symbols across {len(self.exchanges)} exchanges")

    def _collect_funding_rates(self):
        """采集资金费率数据并存储到数据库"""
        symbols = self._get_monitored_symbols()
        timestamp = int(time.time() * 1000)

        for symbol in symbols:
            for exchange_name, exchange in self.exchanges.items():
                try:
                    funding_data = exchange.get_funding_rate(symbol)
                    if funding_data and funding_data.get('funding_rate') is not None:
                        # 更新内存中的数据
                        if symbol not in self.market_data:
                            self.market_data[symbol] = {}
                        if exchange_name not in self.market_data[symbol]:
                            self.market_data[symbol][exchange_name] = {}

                        self.market_data[symbol][exchange_name]['funding_rate'] = funding_data.get('funding_rate')
                        self.market_data[symbol][exchange_name]['predicted_funding_rate'] = funding_data.get('predicted_rate')
                        self.market_data[symbol][exchange_name]['next_funding_time'] = funding_data.get('next_funding_time')

                        # 存储到数据库
                        self.db.execute_query(
                            """
                            INSERT INTO funding_rates (exchange, symbol, timestamp, funding_rate, next_funding_time)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                                funding_rate = excluded.funding_rate,
                                next_funding_time = excluded.next_funding_time
                            """,
                            (
                                exchange_name,
                                symbol,
                                timestamp,
                                funding_data.get('funding_rate'),
                                funding_data.get('next_funding_time')
                            )
                        )

                except Exception as e:
                    logger.debug(f"Error collecting funding rate for {symbol} on {exchange_name}: {e}")

        logger.info(f"Collected funding rates for {len(symbols)} symbols")

    def _get_monitored_symbols(self) -> List[str]:
        """获取需要监控的交易对列表"""
        # 从数据库获取活跃的交易对配置
        configs = self.db.execute_query(
            "SELECT DISTINCT symbol FROM trading_pair_configs WHERE is_active = TRUE"
        )

        if configs:
            return [cfg['symbol'] for cfg in configs]
        else:
            # 如果没有配置，从交易所获取所有USDT永续合约交易对
            return self._get_all_usdt_perpetual_symbols()

    def _get_all_usdt_perpetual_symbols(self) -> List[str]:
        """从交易所获取所有USDT永续合约交易对"""
        all_symbols = set()

        # 从所有已连接的交易所获取交易对
        for exchange_name, exchange in self.exchanges.items():
            try:
                # 加载市场数据
                markets = exchange.exchange.load_markets()

                # 筛选USDT永续合约
                for symbol, market in markets.items():
                    # 检查是否为永续合约且以USDT结算
                    is_perpetual = market.get('type') == 'swap' or market.get('swap') is True
                    is_usdt = symbol.endswith('/USDT:USDT') or (
                        symbol.endswith('/USDT') and is_perpetual
                    )

                    if is_usdt and is_perpetual:
                        # 转换为标准格式 BTC/USDT
                        base_symbol = symbol.split(':')[0] if ':' in symbol else symbol
                        all_symbols.add(base_symbol)

            except Exception as e:
                logger.warning(f"Error loading markets from {exchange_name}: {e}")

        # 转换为列表并排序
        symbols_list = sorted(list(all_symbols))

        if symbols_list:
            logger.info(f"Found {len(symbols_list)} USDT perpetual symbols across {len(self.exchanges)} exchanges")
        else:
            # 如果没有获取到任何交易对，返回默认列表
            logger.warning("No USDT perpetual symbols found, using default symbols")
            symbols_list = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']

        return symbols_list

    def get_market_data(self, symbol: str = None, exchange: str = None) -> Dict[str, Any]:
        """
        获取市场数据
        如果指定symbol和exchange，返回该交易对在该交易所的数据
        如果只指定symbol，返回该交易对在所有交易所的数据
        如果都不指定，返回所有数据
        """
        if symbol and exchange:
            return self.market_data.get(symbol, {}).get(exchange, {})
        elif symbol:
            return self.market_data.get(symbol, {})
        else:
            return self.market_data

    def import_historical_klines(self, file_path: str, exchange: str, symbol: str, timeframe: str):
        """
        从CSV导入历史K线数据
        CSV格式: timestamp, open, high, low, close, volume
        """
        logger.info(f"Importing historical klines from {file_path}...")
        import csv

        count = 0
        with open(file_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    self.db.execute_query(
                        """
                        INSERT INTO klines (exchange, symbol, timeframe, timestamp, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timeframe, timestamp) DO NOTHING
                        """,
                        (
                            exchange,
                            symbol,
                            timeframe,
                            int(row['timestamp']),
                            float(row['open']),
                            float(row['high']),
                            float(row['low']),
                            float(row['close']),
                            float(row['volume'])
                        )
                    )
                    count += 1
                except Exception as e:
                    logger.error(f"Error importing row: {e}")

        logger.info(f"Imported {count} klines records")
        return count

    def import_historical_funding_rates(self, file_path: str, exchange: str, symbol: str):
        """
        从CSV导入历史资金费率数据
        CSV格式: timestamp, funding_rate
        """
        logger.info(f"Importing historical funding rates from {file_path}...")
        import csv

        count = 0
        with open(file_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    self.db.execute_query(
                        """
                        INSERT INTO funding_rates (exchange, symbol, timestamp, funding_rate, next_funding_time)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timestamp) DO NOTHING
                        """,
                        (
                            exchange,
                            symbol,
                            int(row['timestamp']),
                            float(row['funding_rate']),
                            int(row.get('next_funding_time', 0))
                        )
                    )
                    count += 1
                except Exception as e:
                    logger.error(f"Error importing row: {e}")

        logger.info(f"Imported {count} funding rate records")
        return count
