"""
数据采集器
实时采集交易所数据：价格、资金费率、订单簿深度等
"""
import time
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime
from loguru import logger
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        self.exchange_symbols = {}  # 每个交易所支持的交易对: {exchange: {'futures': set(), 'spot': set()}}
        self.market_data = {}  # 存储最新的市场数据
        self.trading_fees_cache = {}  # 缓存交易手续费: {exchange: {symbol: {'maker': float, 'taker': float}}}
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
                    # 缓存该交易所支持的交易对列表
                    self._cache_exchange_symbols(exchange_name)
                else:
                    logger.warning(f"⚠️ {exchange_name.capitalize()} connection test failed")
                    del self.exchanges[exchange_name]

            except Exception as e:
                logger.error(f"Failed to initialize {exchange_name}: {e}")

        logger.info(f"Connected to {len(self.exchanges)} exchanges: {list(self.exchanges.keys())}")

    def _cache_exchange_symbols(self, exchange_name: str):
        """缓存交易所支持的现货和USDT永续合约交易对"""
        try:
            exchange = self.exchanges.get(exchange_name)
            if not exchange:
                return
            
            markets = exchange.exchange.load_markets()
            futures_symbols = set()
            spot_symbols = set()
            
            for symbol, market in markets.items():
                # 检查是否为永续合约且以USDT结算
                is_perpetual = market.get('type') == 'swap' or market.get('swap') is True
                is_usdt_futures = symbol.endswith('/USDT:USDT') or (
                    symbol.endswith('/USDT') and is_perpetual
                )
                
                if is_usdt_futures and is_perpetual:
                    # 转换为标准格式 BTC/USDT
                    base_symbol = symbol.split(':')[0] if ':' in symbol else symbol
                    futures_symbols.add(base_symbol)
                
                # 检查是否为现货且以USDT报价
                is_spot = market.get('type') == 'spot'
                is_usdt_spot = symbol.endswith('/USDT') and is_spot
                
                if is_usdt_spot:
                    spot_symbols.add(symbol)
            
            self.exchange_symbols[exchange_name] = {
                'futures': futures_symbols,
                'spot': spot_symbols
            }
            logger.info(f"Cached {len(futures_symbols)} futures and {len(spot_symbols)} spot symbols for {exchange_name}")
            
            # 缓存手续费（只缓存同时有现货和期货的币种）
            self._cache_trading_fees(exchange_name, futures_symbols & spot_symbols)
            
        except Exception as e:
            logger.error(f"Error caching symbols for {exchange_name}: {e}")
            self.exchange_symbols[exchange_name] = {'futures': set(), 'spot': set()}

    def _cache_trading_fees(self, exchange_name: str, symbols: set):
        """缓存交易手续费"""
        if exchange_name not in self.trading_fees_cache:
            self.trading_fees_cache[exchange_name] = {}
        
        exchange = self.exchanges.get(exchange_name)
        if not exchange:
            return
        
        cached_count = 0
        for symbol in symbols:
            try:
                fees = exchange.get_trading_fees(symbol)
                self.trading_fees_cache[exchange_name][symbol] = {
                    'maker': fees.get('maker', 0.001),
                    'taker': fees.get('taker', 0.001)
                }
                cached_count += 1
            except Exception as e:
                logger.debug(f"Error caching fees for {symbol} on {exchange_name}: {e}")
        
        logger.info(f"Cached trading fees for {cached_count} symbols on {exchange_name}")

    def reload_exchanges(self):
        """重新加载交易所连接（支持热更新）"""
        logger.info("Reloading exchange connections...")
        
        # 重新加载账户信息
        self.account_manager.reload_accounts()
        
        # 关闭旧连接
        self.exchanges.clear()
        self.exchange_symbols.clear()
        self.trading_fees_cache.clear()
        
        # 重新初始化
        self._init_exchanges()
        
        logger.info("Exchange connections reloaded")

    def start(self):
        """启动数据采集"""
        logger.info("Starting data collector...")
        self.running = True
        
        # 启动前先从数据库加载最近的数据（避免冷启动无数据）
        self._load_recent_data_from_db()

        # 启动价格采集线程
        threading.Thread(target=self._price_collection_loop, daemon=True).start()

        # 启动资金费率采集线程
        threading.Thread(target=self._funding_rate_collection_loop, daemon=True).start()

        logger.info("Data collector started")

    def stop(self):
        """停止数据采集"""
        logger.info("Stopping data collector...")
        self.running = False

    def _load_recent_data_from_db(self, max_age_minutes: int = 10):
        """从数据库加载最近的数据到内存（程序启动时使用）"""
        try:
            current_time = int(time.time() * 1000)
            min_timestamp = current_time - (max_age_minutes * 60 * 1000)
            
            # 加载价格数据
            price_rows = self.db.execute_query(
                """
                SELECT exchange, symbol, timestamp,
                       spot_bid, spot_ask, spot_price,
                       futures_bid, futures_ask, futures_price,
                       maker_fee, taker_fee
                FROM market_prices
                WHERE timestamp > ?
                """,
                (min_timestamp,)
            )
            
            # 加载资金费率数据
            funding_rows = self.db.execute_query(
                """
                SELECT exchange, symbol, funding_rate, next_funding_time, timestamp
                FROM funding_rates
                WHERE timestamp > ?
                """,
                (min_timestamp,)
            )
            
            # 构建market_data
            loaded_count = 0
            for row in price_rows:
                symbol = row['symbol']
                exchange = row['exchange']
                
                if symbol not in self.market_data:
                    self.market_data[symbol] = {}
                if exchange not in self.market_data[symbol]:
                    self.market_data[symbol][exchange] = {}
                
                self.market_data[symbol][exchange].update({
                    'spot_bid': row['spot_bid'],
                    'spot_ask': row['spot_ask'],
                    'spot_price': row['spot_price'],
                    'futures_bid': row['futures_bid'],
                    'futures_ask': row['futures_ask'],
                    'futures_price': row['futures_price'],
                    'maker_fee': row['maker_fee'],
                    'taker_fee': row['taker_fee'],
                    'timestamp': row['timestamp']
                })
                loaded_count += 1
            
            # 合并资金费率数据
            for row in funding_rows:
                symbol = row['symbol']
                exchange = row['exchange']
                
                if symbol not in self.market_data:
                    self.market_data[symbol] = {}
                if exchange not in self.market_data[symbol]:
                    self.market_data[symbol][exchange] = {}
                
                self.market_data[symbol][exchange].update({
                    'funding_rate': row['funding_rate'],
                    'next_funding_time': row['next_funding_time']
                })
            
            logger.info(f"从数据库加载了 {len(self.market_data)} 个币种的历史数据（最近{max_age_minutes}分钟）")
            
        except Exception as e:
            logger.error(f"从数据库加载历史数据失败: {e}")

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
        """采集所有交易对的价格数据（批量模式，采集所有期货币种+现货币种）"""
        start_time = time.time()
        
        for exchange_name, exchange in self.exchanges.items():
            try:
                exchange_support = self.exchange_symbols.get(exchange_name, {'futures': set(), 'spot': set()})
                futures_symbols = exchange_support.get('futures', set())
                spot_symbols = exchange_support.get('spot', set())
                
                if not futures_symbols:
                    continue
                
                logger.info(f"开始批量采集 {exchange_name}: {len(futures_symbols)} 个期货, {len(spot_symbols)} 个现货...")
                
                # ⚡ 批量获取所有期货ticker（1次API调用）
                futures_tickers = {}
                try:
                    all_futures_tickers = exchange.exchange.fetch_tickers(params={'type': 'swap'})
                    for symbol, ticker in all_futures_tickers.items():
                        base_symbol = symbol.split(':')[0] if ':' in symbol else symbol
                        if base_symbol in futures_symbols:
                            futures_tickers[base_symbol] = ticker
                    logger.info(f"批量获取了 {len(futures_tickers)} 个期货ticker")
                except Exception as e:
                    logger.warning(f"批量获取期货ticker失败: {e}")
                
                # ⚡ 批量获取所有现货ticker（1次API调用）
                spot_tickers = {}
                try:
                    all_spot_tickers = exchange.exchange.fetch_tickers(params={'type': 'spot'})
                    spot_tickers = {k: v for k, v in all_spot_tickers.items() if k in spot_symbols}
                    logger.info(f"批量获取了 {len(spot_tickers)} 个现货ticker")
                except Exception as e:
                    logger.warning(f"批量获取现货ticker失败: {e}")
                
                # 处理每个期货币种的数据
                success_count = 0
                error_count = 0
                
                for symbol in futures_symbols:
                    try:
                        # 初始化数据结构
                        if symbol not in self.market_data:
                            self.market_data[symbol] = {}
                        if exchange_name not in self.market_data[symbol]:
                            self.market_data[symbol][exchange_name] = {}
                        
                        # 期货数据（必须有）
                        if symbol in futures_tickers:
                            ticker = futures_tickers[symbol]
                            self.market_data[symbol][exchange_name]['futures_bid'] = ticker.get('bid')
                            self.market_data[symbol][exchange_name]['futures_ask'] = ticker.get('ask')
                            self.market_data[symbol][exchange_name]['futures_price'] = ticker.get('last')
                        
                        # 现货数据（如果有）
                        if symbol in spot_tickers:
                            ticker = spot_tickers[symbol]
                            self.market_data[symbol][exchange_name]['spot_bid'] = ticker.get('bid')
                            self.market_data[symbol][exchange_name]['spot_ask'] = ticker.get('ask')
                            self.market_data[symbol][exchange_name]['spot_price'] = ticker.get('last')
                        
                        # 使用缓存的手续费
                        cached_fees = self.trading_fees_cache.get(exchange_name, {}).get(symbol)
                        if cached_fees:
                            self.market_data[symbol][exchange_name]['maker_fee'] = cached_fees['maker']
                            self.market_data[symbol][exchange_name]['taker_fee'] = cached_fees['taker']
                        else:
                            self.market_data[symbol][exchange_name]['maker_fee'] = 0.001
                            self.market_data[symbol][exchange_name]['taker_fee'] = 0.001
                        
                        # 添加时间戳
                        timestamp = int(time.time() * 1000)
                        self.market_data[symbol][exchange_name]['timestamp'] = timestamp
                        
                        # 存储到数据库
                        self.db.execute_query(
                            """
                            INSERT INTO market_prices (
                                exchange, symbol, timestamp,
                                spot_bid, spot_ask, spot_price,
                                futures_bid, futures_ask, futures_price,
                                maker_fee, taker_fee
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                                spot_bid = excluded.spot_bid,
                                spot_ask = excluded.spot_ask,
                                spot_price = excluded.spot_price,
                                futures_bid = excluded.futures_bid,
                                futures_ask = excluded.futures_ask,
                                futures_price = excluded.futures_price,
                                maker_fee = excluded.maker_fee,
                                taker_fee = excluded.taker_fee
                            """,
                            (
                                exchange_name,
                                symbol,
                                timestamp,
                                self.market_data[symbol][exchange_name].get('spot_bid'),
                                self.market_data[symbol][exchange_name].get('spot_ask'),
                                self.market_data[symbol][exchange_name].get('spot_price'),
                                self.market_data[symbol][exchange_name].get('futures_bid'),
                                self.market_data[symbol][exchange_name].get('futures_ask'),
                                self.market_data[symbol][exchange_name].get('futures_price'),
                                self.market_data[symbol][exchange_name].get('maker_fee'),
                                self.market_data[symbol][exchange_name].get('taker_fee')
                            )
                        )
                        
                        success_count += 1
                        
                    except Exception as e:
                        error_count += 1
                        logger.debug(f"处理 {symbol} 数据失败: {e}")
                
                elapsed = time.time() - start_time
                logger.info(f"{exchange_name} 采集完成: {len(futures_symbols)} 个期货币种, 成功 {success_count}, 失败 {error_count}, 耗时 {elapsed:.2f}秒")
                
            except Exception as e:
                logger.error(f"采集 {exchange_name} 价格数据失败: {e}")

    def _collect_funding_rates(self):
        """采集资金费率数据并存储到数据库（批量模式）"""
        timestamp = int(time.time() * 1000)
        
        for exchange_name, exchange in self.exchanges.items():
            start_time = time.time()
            try:
                # 获取该交易所支持期货的币种
                exchange_support = self.exchange_symbols.get(exchange_name, {'futures': set(), 'spot': set()})
                futures_symbols = exchange_support.get('futures', set())
                
                if not futures_symbols:
                    continue
                
                total = len(futures_symbols)
                logger.info(f"开始并发采集 {exchange_name} 的 {total} 个币种资金费率...")
                
                success_count = 0
                error_count = 0
                
                def fetch_funding_rate(symbol):
                    """获取单个币种的资金费率"""
                    try:
                        return symbol, exchange.get_funding_rate(symbol)
                    except Exception as e:
                        logger.debug(f"{symbol} 获取失败: {e}")
                        return symbol, None
                
                # 使用线程池并发获取（10个并发线程）
                with ThreadPoolExecutor(max_workers=10) as executor:
                    future_tasks = {executor.submit(fetch_funding_rate, sym): sym for sym in futures_symbols}
                    
                    for idx, future in enumerate(as_completed(future_tasks), 1):
                        symbol, funding_data = future.result()
                        
                        if funding_data and funding_data.get('funding_rate') is not None:
                            # 更新内存
                            if symbol not in self.market_data:
                                self.market_data[symbol] = {}
                            if exchange_name not in self.market_data[symbol]:
                                self.market_data[symbol][exchange_name] = {}
                            
                            self.market_data[symbol][exchange_name].update(funding_data)
                            
                            # 存储到数据库
                            try:
                                self.db.execute_query(
                                    """
                                    INSERT INTO funding_rates (exchange, symbol, timestamp, funding_rate, next_funding_time, funding_interval)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                                        funding_rate = excluded.funding_rate,
                                        next_funding_time = excluded.next_funding_time,
                                        funding_interval = excluded.funding_interval
                                    """,
                                    (exchange_name, symbol, timestamp, 
                                     funding_data.get('funding_rate'),
                                     funding_data.get('next_funding_time'),
                                     funding_data.get('funding_interval'))
                                )
                                success_count += 1
                            except Exception as e:
                                error_count += 1
                                logger.debug(f"{symbol} 数据库插入失败: {e}")
                        else:
                            error_count += 1
                        
                        # 每100个币种打印进度
                        if idx % 100 == 0 or idx == total:
                            logger.info(f"进度: {idx}/{total} ({idx*100//total}%) - 成功: {success_count}, 失败: {error_count}")
                
                elapsed = time.time() - start_time
                logger.info(f"{exchange_name} 资金费率采集完成: {len(futures_symbols)} 个币种, 成功 {success_count}, 失败 {error_count}, 耗时 {elapsed:.2f}秒")
                
            except Exception as e:
                logger.error(f"采集 {exchange_name} 资金费率时出错: {e}")

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

    def get_market_data(self, symbol: Optional[str] = None, exchange: Optional[str] = None) -> Dict[str, Any]:
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
                        INSERT INTO funding_rates (exchange, symbol, timestamp, funding_rate, next_funding_time, funding_interval)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timestamp) DO NOTHING
                        """,
                        (
                            exchange,
                            symbol,
                            int(row['timestamp']),
                            float(row['funding_rate']),
                            int(row.get('next_funding_time', 0)),
                            int(row.get('funding_interval', 0))
                        )
                    )
                    count += 1
                except Exception as e:
                    logger.error(f"Error importing row: {e}")

        logger.info(f"Imported {count} funding rate records")
        return count
