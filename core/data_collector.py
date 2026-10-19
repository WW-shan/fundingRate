"""
数据采集器（监控版）
仅采集公开市场数据：资金费率、期货/现货价格、稳定币价格
使用批量接口，单次调用获取全市场数据
"""
import os
import time
import threading
from typing import Dict, List, Any, Optional
from loguru import logger
from config import ConfigManager
from database import DatabaseManager
from exchanges import EXCHANGE_CLASSES


class DataCollector:
    """市场数据采集器（纯监控，无交易）"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.running = False
        self.exchanges: Dict[str, Any] = {}
        self.market_data: Dict[str, Dict[str, Any]] = {}   # {symbol: {exchange: {...}}}
        self.stablecoin_data: Dict[str, Dict[str, Any]] = {}  # {symbol: {exchange: {...}}}
        self._init_exchanges()

    # ---------- 初始化 ----------

    def _get_proxy(self, exchange_name: str) -> str:
        """按交易所读取代理配置（环境变量优先，其次全局配置）"""
        env_key = f"{exchange_name.upper()}_PROXY"
        proxy = os.getenv(env_key) or os.getenv('PROXY_URL') or ''
        if not proxy:
            proxy = self.config.get('global', f'{exchange_name}_proxy', '') or ''
        return proxy or ''

    def _init_exchanges(self):
        """初始化所有已启用的交易所（公开 API，无需密钥）"""
        logger.info("Initializing exchange connections (public API only)...")
        enabled = self.config.get('global', 'enabled_exchanges',
                                  ['binance', 'okx', 'bybit', 'gate', 'bitget'])
        if isinstance(enabled, str):
            enabled = [e.strip() for e in enabled.split(',') if e.strip()]

        for name in enabled:
            name = name.lower()
            cls = EXCHANGE_CLASSES.get(name)
            if not cls:
                logger.warning(f"Unknown exchange: {name}")
                continue
            try:
                proxy = self._get_proxy(name)
                adapter = cls(proxy=proxy)
                if adapter.test_connection():
                    self.exchanges[name] = adapter
                    logger.info(f"{name} connected (proxy={'yes' if proxy else 'no'})")
                else:
                    logger.warning(f"{name} connection failed, skipped")
            except Exception as e:
                logger.error(f"Failed to init {name}: {e}")

        logger.info(f"Active exchanges: {list(self.exchanges.keys())}")

    def reload_exchanges(self):
        """重新加载交易所连接（热更新）"""
        logger.info("Reloading exchange connections...")
        self.exchanges.clear()
        self._init_exchanges()

    # ---------- 生命周期 ----------

    def start(self):
        self.running = True
        threading.Thread(target=self._funding_loop, daemon=True).start()
        threading.Thread(target=self._stablecoin_loop, daemon=True).start()
        threading.Thread(target=self._price_loop, daemon=True).start()
        logger.info("Data collector started (funding + stablecoin + price loops)")

    def stop(self):
        self.running = False

    # ---------- 资金费率采集 ----------

    def _funding_loop(self):
        interval = int(self.config.get('global', 'funding_refresh_interval', 300))
        while self.running:
            try:
                self._collect_funding_rates()
            except Exception as e:
                logger.error(f"Funding collection error: {e}")
            time.sleep(interval)

    def _collect_funding_rates(self):
        """批量采集全市场资金费率（每家 1 次 API 调用）"""
        for name, adapter in self.exchanges.items():
            try:
                t0 = time.time()
                rates = adapter.exchange.fetch_funding_rates()

                # 部分交易所（Binance/Bitget）批量接口缺 interval/fundingTimestamp，
                # 用官方 fundingIntervals 接口补充（Binance 权重 0，一次全量）
                intervals = {}
                if adapter.exchange.has.get('fetchFundingIntervals'):
                    try:
                        intervals = adapter.exchange.fetch_funding_intervals()
                    except Exception as e:
                        logger.debug(f"{name} fetch_funding_intervals failed: {e}")

                count = 0
                ts = int(time.time() * 1000)

                for symbol, data in rates.items():
                    # 只保留 USDT 本位永续（统一为 BTC/USDT 格式）
                    if not symbol.endswith(':USDT'):
                        continue
                    base_symbol = symbol.split(':')[0]
                    rate = data.get('fundingRate')
                    if rate is None:
                        continue

                    iv_data = intervals.get(symbol) or {}
                    interval_ms = (adapter._parse_funding_interval(data)
                                   or adapter._parse_funding_interval(iv_data))
                    next_ts = data.get('fundingTimestamp') or iv_data.get('fundingTimestamp')

                    entry = {
                        'funding_rate': float(rate),
                        'next_funding_time': next_ts,
                        'funding_interval': interval_ms,
                        'rate_semantics': adapter.funding_rate_semantics,
                        'timestamp': ts,
                    }
                    self.market_data.setdefault(base_symbol, {}).setdefault(name, {}).update(entry)

                    self.db.execute_query(
                        """
                        INSERT INTO funding_rates (exchange, symbol, timestamp, funding_rate, next_funding_time, funding_interval)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                            funding_rate = excluded.funding_rate,
                            next_funding_time = excluded.next_funding_time,
                            funding_interval = excluded.funding_interval
                        """,
                        (name, base_symbol, ts, entry['funding_rate'], next_ts, interval_ms)
                    )
                    count += 1

                logger.info(f"{name}: collected {count} funding rates in {time.time()-t0:.1f}s")
            except Exception as e:
                logger.error(f"{name} funding collection failed: {e}")

    # ---------- 价格采集（期现基差用） ----------

    def _price_loop(self):
        interval = int(self.config.get('global', 'price_refresh_interval', 30))
        while self.running:
            try:
                self._collect_prices()
            except Exception as e:
                logger.error(f"Price collection error: {e}")
            time.sleep(interval)

    def _collect_prices(self):
        """批量采集现货+永续价格（每家各 1 次批量调用）"""
        ts = int(time.time() * 1000)
        for name, adapter in self.exchanges.items():
            try:
                spot_tickers = {}
                futures_tickers = {}
                try:
                    spot_tickers = adapter.exchange.fetch_tickers(params={'type': 'spot'})
                except Exception as e:
                    logger.debug(f"{name} spot tickers failed: {e}")
                try:
                    futures_tickers = adapter.exchange.fetch_tickers(params={'type': 'swap'})
                except Exception as e:
                    logger.debug(f"{name} swap tickers failed: {e}")

                # 收集有现货且有永续的币种
                symbols = set()
                for sym in spot_tickers:
                    if sym.endswith('/USDT') and ':' not in sym:
                        symbols.add(sym)
                futures_map = {}
                for sym, t in futures_tickers.items():
                    if sym.endswith(':USDT'):
                        futures_map[sym.split(':')[0]] = t

                count = 0
                stale_ms = int(self.config.get('global', 'price_stale_threshold', 600)) * 1000
                now_ms = int(time.time() * 1000)
                for sym in symbols:
                    if sym not in futures_map:
                        continue
                    st = spot_tickers[sym]
                    ft = futures_map[sym]

                    # 过滤陈旧行情（退市/停牌币种的 ticker 会保留最后价格）
                    st_ts = st.get('timestamp')
                    ft_ts = ft.get('timestamp')
                    if st_ts and (now_ms - st_ts) > stale_ms:
                        continue
                    if ft_ts and (now_ms - ft_ts) > stale_ms:
                        continue

                    spot_price = st.get('last') or st.get('bid')
                    futures_price = ft.get('last') or ft.get('bid')
                    if not spot_price or not futures_price:
                        continue

                    entry = {
                        'spot_bid': st.get('bid'),
                        'spot_ask': st.get('ask'),
                        'spot_price': spot_price,
                        'futures_bid': ft.get('bid'),
                        'futures_ask': ft.get('ask'),
                        'futures_price': futures_price,
                        'timestamp': ts,
                    }
                    self.market_data.setdefault(sym, {}).setdefault(name, {}).update(entry)

                    self.db.execute_query(
                        """
                        INSERT INTO market_prices (exchange, symbol, timestamp,
                            spot_bid, spot_ask, spot_price, futures_bid, futures_ask, futures_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                            spot_bid = excluded.spot_bid, spot_ask = excluded.spot_ask,
                            spot_price = excluded.spot_price,
                            futures_bid = excluded.futures_bid, futures_ask = excluded.futures_ask,
                            futures_price = excluded.futures_price
                        """,
                        (name, sym, ts, entry['spot_bid'], entry['spot_ask'], spot_price,
                         entry['futures_bid'], entry['futures_ask'], futures_price)
                    )
                    count += 1
                logger.info(f"{name}: collected {count} spot+futures price pairs")
            except Exception as e:
                logger.error(f"{name} price collection failed: {e}")

    # ---------- 稳定币采集 ----------

    def _stablecoin_loop(self):
        interval = int(self.config.get('global', 'stablecoin_refresh_interval', 60))
        while self.running:
            try:
                self._collect_stablecoins()
            except Exception as e:
                logger.error(f"Stablecoin collection error: {e}")
            time.sleep(interval)

    def _collect_stablecoins(self):
        """采集稳定币现货价格（用于脱锚监控）"""
        symbols = self.config.get('global', 'stablecoin_symbols',
                                  ['USDC/USDT', 'FDUSD/USDT', 'TUSD/USDT', 'DAI/USDT', 'USDE/USDT'])
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(',') if s.strip()]

        ts = int(time.time() * 1000)
        for name, adapter in self.exchanges.items():
            for symbol in symbols:
                try:
                    ticker = adapter.get_spot_ticker(symbol)
                    price = ticker.get('last') or ticker.get('bid')
                    if not price:
                        continue
                    price = float(price)
                    self.stablecoin_data.setdefault(symbol, {})[name] = {
                        'price': price,
                        'bid': ticker.get('bid'),
                        'ask': ticker.get('ask'),
                        'timestamp': ts,
                    }
                    self.db.execute_query(
                        """
                        INSERT INTO stablecoin_prices (exchange, symbol, timestamp, price, bid, ask)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                            price = excluded.price, bid = excluded.bid, ask = excluded.ask
                        """,
                        (name, symbol, ts, price, ticker.get('bid'), ticker.get('ask'))
                    )
                except Exception as e:
                    logger.debug(f"{name} {symbol} stablecoin fetch failed: {e}")

        logger.debug(f"Stablecoin data collected: {len(self.stablecoin_data)} symbols")

    # ---------- 供监控模块使用 ----------

    def get_market_data(self) -> Dict[str, Dict[str, Any]]:
        return self.market_data

    def get_stablecoin_data(self) -> Dict[str, Dict[str, Any]]:
        return self.stablecoin_data

    def get_status(self) -> Dict[str, Any]:
        return {
            'running': self.running,
            'exchanges': list(self.exchanges.keys()),
            'symbols_tracked': len(self.market_data),
            'stablecoins_tracked': len(self.stablecoin_data),
        }
