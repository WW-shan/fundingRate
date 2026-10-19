"""
数据采集器（监控版）
仅采集公开市场数据：资金费率、期货/现货价格、稳定币价格
使用批量接口，单次调用获取全市场数据
"""
import os
import time
import threading
from typing import Dict, Any
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
        self.reference_prices: Dict[str, Dict[str, Any]] = {}  # {yahoo_symbol: {...}}
        from config.peg_assets import PegAssetDiscovery
        self.peg_discovery = PegAssetDiscovery(config_manager)
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

        new_exchanges = {}
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
                    new_exchanges[name] = adapter
                    logger.info(f"{name} connected (proxy={'yes' if proxy else 'no'})")
                else:
                    logger.warning(f"{name} connection failed, skipped")
            except Exception as e:
                logger.error(f"Failed to init {name}: {e}")

        # 原子替换，避免采集线程在 clear/repopulate 期间遍历字典报错
        self.exchanges = new_exchanges
        logger.info(f"Active exchanges: {list(self.exchanges.keys())}")

    def reload_exchanges(self):
        """重新加载交易所连接（热更新）"""
        logger.info("Reloading exchange connections...")
        self._init_exchanges()

    # ---------- 生命周期 ----------

    def start(self):
        self.running = True
        threading.Thread(target=self._funding_loop, daemon=True).start()
        threading.Thread(target=self._stablecoin_loop, daemon=True).start()
        threading.Thread(target=self._price_loop, daemon=True).start()
        threading.Thread(target=self._reference_loop, daemon=True).start()
        logger.info("Data collector started (funding + peg + price + reference loops)")

    def stop(self):
        self.running = False

    # ---------- 资金费率采集 ----------

    def _funding_loop(self):
        while self.running:
            interval = int(self.config.get('global', 'funding_refresh_interval', 300))
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
                # Fetch slower interval metadata first; fetch the live predicted rate last.
                intervals = {}
                if adapter.exchange.has.get('fetchFundingIntervals'):
                    try:
                        intervals = adapter.exchange.fetch_funding_intervals()
                    except Exception as e:
                        logger.debug(f"{name} fetch_funding_intervals failed: {e}")

                rates = adapter.exchange.fetch_funding_rates()
                if not rates:
                    logger.warning(f"{name} returned no funding rates; keeping previous snapshot")
                    continue

                count = 0
                ts = int(time.time() * 1000)
                db_rows = []

                # 只移除该交易所上一轮资金费率字段，保留价格字段
                funding_fields = ('funding_rate', 'next_funding_time', 'funding_interval',
                                  'rate_semantics', 'funding_timestamp')
                for exchanges in self.market_data.values():
                    entry = exchanges.get(name)
                    if entry:
                        for key in funding_fields:
                            entry.pop(key, None)

                for symbol, data in rates.items():
                    # 只保留 USDT 本位永续（统一为 BTC/USDT 格式）
                    if not symbol.endswith(':USDT'):
                        continue
                    # 跳过非活跃市场（Binance SETTLING/退市合约的 lastFundingRate 是陈旧值）
                    try:
                        market = adapter.exchange.market(symbol)
                        if market.get('active') is False:
                            continue
                    except Exception:
                        pass
                    base_symbol = adapter.canonical_symbol(symbol)
                    rate = data.get('fundingRate')
                    if rate is None:
                        continue

                    iv_data = intervals.get(symbol) or {}
                    interval_ms = (adapter._parse_funding_interval(data)
                                   or adapter._parse_funding_interval(iv_data)
                                   or adapter.default_funding_interval_ms)
                    next_ts = data.get('fundingTimestamp') or iv_data.get('fundingTimestamp')

                    entry = {
                        'funding_rate': float(rate),
                        'next_funding_time': next_ts,
                        'funding_interval': interval_ms,
                        'rate_semantics': adapter.funding_rate_semantics,
                        'funding_timestamp': ts,
                    }
                    self.market_data.setdefault(base_symbol, {}).setdefault(name, {}).update(entry)
                    db_rows.append((name, base_symbol, ts, entry['funding_rate'], next_ts, interval_ms))
                    count += 1

                for sym, exchanges in list(self.market_data.items()):
                    for ex_name, entry in list(exchanges.items()):
                        if not entry:
                            del exchanges[ex_name]
                    if not exchanges:
                        del self.market_data[sym]

                self.db.execute_many(
                    """
                    INSERT INTO funding_rates (exchange, symbol, timestamp, funding_rate, next_funding_time, funding_interval)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                        funding_rate = excluded.funding_rate,
                        next_funding_time = excluded.next_funding_time,
                        funding_interval = excluded.funding_interval
                    """,
                    db_rows
                )
                logger.info(f"{name}: collected {count} funding rates in {time.time()-t0:.1f}s")
            except Exception as e:
                logger.error(f"{name} funding collection failed: {e}")

    # ---------- 价格采集（期现基差用） ----------

    def _price_loop(self):
        time.sleep(5)
        while self.running:
            interval = int(self.config.get('global', 'price_refresh_interval', 30))
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

                if not spot_tickers or not futures_tickers:
                    logger.warning(f"{name} price tickers incomplete; keeping previous snapshot")
                    continue

                # 收集有现货且有永续的币种
                spot_map = {}
                for sym, ticker in spot_tickers.items():
                    if sym.endswith('/USDT') and ':' not in sym:
                        canonical = adapter.canonical_symbol(sym)
                        spot_map[canonical] = (sym, ticker)
                futures_map = {}
                for sym, ticker in futures_tickers.items():
                    if sym.endswith(':USDT'):
                        canonical = adapter.canonical_symbol(sym)
                        futures_map[canonical] = (sym, ticker)

                count = 0
                db_rows = []
                # 只移除该交易所上一轮价格字段，保留资金费率字段
                price_fields = ('spot_bid', 'spot_ask', 'spot_price', 'futures_bid',
                                'futures_ask', 'futures_price', 'timestamp')
                for exchanges in self.market_data.values():
                    entry = exchanges.get(name)
                    if entry:
                        for key in price_fields:
                            entry.pop(key, None)
                stale_ms = int(self.config.get('global', 'price_stale_threshold', 600)) * 1000
                now_ms = int(time.time() * 1000)
                for sym in spot_map.keys() & futures_map.keys():
                    spot_symbol, st = spot_map[sym]
                    futures_symbol, ft = futures_map[sym]
                    try:
                        if adapter.exchange.market(spot_symbol).get('active') is False:
                            continue
                        if adapter.exchange.market(futures_symbol).get('active') is False:
                            continue
                    except Exception:
                        pass

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
                    db_rows.append((name, sym, ts, entry['spot_bid'], entry['spot_ask'], spot_price,
                                    entry['futures_bid'], entry['futures_ask'], futures_price))
                    count += 1

                for sym, exchanges in list(self.market_data.items()):
                    for ex_name, entry in list(exchanges.items()):
                        if not entry:
                            del exchanges[ex_name]
                    if not exchanges:
                        del self.market_data[sym]

                self.db.execute_many(
                    """
                    INSERT INTO market_prices_latest (exchange, symbol, timestamp,
                        spot_bid, spot_ask, spot_price, futures_bid, futures_ask, futures_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(exchange, symbol) DO UPDATE SET
                        timestamp = excluded.timestamp,
                        spot_bid = excluded.spot_bid, spot_ask = excluded.spot_ask,
                        spot_price = excluded.spot_price,
                        futures_bid = excluded.futures_bid, futures_ask = excluded.futures_ask,
                        futures_price = excluded.futures_price
                    """,
                    db_rows
                )
                logger.info(f"{name}: collected {count} spot+futures price pairs")
            except Exception as e:
                logger.error(f"{name} price collection failed: {e}")

    # ---------- 稳定币采集 ----------

    def _stablecoin_loop(self):
        time.sleep(5)
        while self.running:
            interval = int(self.config.get('global', 'stablecoin_refresh_interval', 60))
            try:
                self._collect_stablecoins()
            except Exception as e:
                logger.error(f"Stablecoin collection error: {e}")
            time.sleep(interval)

    def _collect_stablecoins(self):
        """采集锚定资产价格（稳定币/黄金/股票，带流动性过滤）"""
        # 首次运行 registry 为空时主动发现（避免静默跳过）
        registry = self.peg_discovery.refresh(self.exchanges, force=not self.peg_discovery.registry)
        symbols = list(registry.keys())

        if not symbols:
            logger.warning("Peg asset registry empty (all assets filtered out?)")
            return

        ts = int(time.time() * 1000)
        stale_ms = int(self.config.get('global', 'price_stale_threshold', 600)) * 1000
        collected = 0
        db_rows = []
        new_data = {}
        wanted = {f'{sym}/USDT': sym for sym in symbols}
        for name, adapter in self.exchanges.items():
            try:
                tickers = adapter.exchange.fetch_tickers(params={'type': 'spot'})
            except Exception as e:
                logger.debug(f"{name} peg tickers failed: {e}")
                continue
            canonical_tickers = {}
            for ticker_symbol, ticker in tickers.items():
                if ticker_symbol.endswith('/USDT') and ':' not in ticker_symbol:
                    canonical_tickers[adapter.canonical_symbol(ticker_symbol)] = (ticker_symbol, ticker)
            for market_symbol, sym in wanted.items():
                lookup_symbol = 'USDC/USDT' if sym == 'USDT' else market_symbol
                item = canonical_tickers.get(lookup_symbol)
                if not item:
                    continue
                actual_market, ticker = item
                try:
                    if adapter.exchange.market(actual_market).get('active') is False:
                        continue
                except Exception:
                    pass
                ticker_ts = ticker.get('timestamp')
                stale = bool(ticker_ts and (ts - ticker_ts) > stale_ms)
                raw_price = ticker.get('last') or ticker.get('bid')
                if stale:
                    # Low-liquidity but still active markets may have a stale last
                    # trade while two-sided bid/ask remain valid. Use mid price.
                    bid = ticker.get('bid')
                    ask = ticker.get('ask')
                    try:
                        bid_f, ask_f = float(bid), float(ask)
                    except (TypeError, ValueError):
                        continue
                    if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
                        continue
                    raw_price = (bid_f + ask_f) / 2
                if not raw_price:
                    continue
                try:
                    raw_price = float(raw_price)
                except (TypeError, ValueError):
                    continue
                if raw_price <= 0:
                    continue
                if sym == 'USDT':
                    # USDC/USDT -> USDT/USD（USDC≈1）；bid/ask 方向相反
                    price = 1.0 / raw_price
                    bid = (1.0 / float(ticker['ask'])) if ticker.get('ask') else None
                    ask = (1.0 / float(ticker['bid'])) if ticker.get('bid') else None
                else:
                    price = raw_price
                    bid = ticker.get('bid')
                    ask = ticker.get('ask')
                new_data.setdefault(sym, {})[name] = {
                    'price': price,
                    'bid': bid,
                    'ask': ask,
                    'timestamp': ticker.get('timestamp') or ts,
                }
                db_rows.append((name, sym, ts, price, bid, ask))
                collected += 1
        if not new_data:
            logger.warning("Peg asset collection returned no quotes; keeping previous snapshot")
            return
        self.stablecoin_data = new_data

        self.db.execute_many(
            """
            INSERT INTO stablecoin_prices (exchange, symbol, timestamp, price, bid, ask)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(exchange, symbol, timestamp) DO UPDATE SET
                price = excluded.price, bid = excluded.bid, ask = excluded.ask
            """,
            db_rows
        )
        logger.info(f"Peg assets collected: {collected} quotes across {len(symbols)} symbols")

    # ---------- 参考价采集（黄金/欧元/股票） ----------

    def _reference_loop(self):
        # 等待锚定资产发现完成，否则首次只会有黄金/欧元两个参考价
        for _ in range(24):
            if not self.running or self.stablecoin_data:
                break
            time.sleep(5)
        while self.running:
            interval = int(self.config.get('global', 'reference_refresh_interval', 300))
            try:
                self._collect_reference_prices()
            except Exception as e:
                logger.error(f"Reference price collection error: {e}")
            time.sleep(interval)

    def _collect_reference_prices(self):
        """从 Yahoo Finance 并发采集参考价（黄金 / EUR-USD / 股票/大宗商品代币）"""
        from config import peg_assets
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import requests

        proxy = os.getenv('PROXY_URL', '') or self.config.get('global', 'reference_proxy', '')
        proxies = {'http': proxy, 'https': proxy} if proxy else None

        # 确保 registry 已初始化（首次运行时为空，需先发现）
        if not self.peg_discovery.registry:
            self.peg_discovery.refresh(self.exchanges, force=True)

        targets = {
            peg_assets.YAHOO_SYMBOLS['gold']: 'gold',
            peg_assets.YAHOO_SYMBOLS['eurusd']: 'eurusd',
        }
        # 只抓当前有报价的资产，并按 Yahoo 代码去重（多个发行商常共用同一参考价）
        active_symbols = set(self.stablecoin_data)
        for sym, entry in self.peg_discovery.registry.items():
            ref = entry.get('reference_symbol')
            if sym in active_symbols and ref and entry.get('category') in ('stock', 'commodity', 'eur_stable'):
                targets[ref] = entry['category']

        ts = int(time.time() * 1000)
        db_rows = []

        def fetch(item):
            yahoo_sym, category = item
            try:
                r = requests.get(
                    f'https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}?interval=1m&range=1d',
                    proxies=proxies, timeout=15,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                result = r.json()['chart']['result'][0]
                price = result['meta'].get('regularMarketPrice')
                if not price:
                    return None
                return yahoo_sym, category, {
                    'price': float(price),
                    'category': category,
                    'timestamp': ts,
                    'market_time': result['meta'].get('regularMarketTime'),
                    'previous_close': result['meta'].get('previousClose'),
                }
            except Exception as e:
                logger.debug(f"Yahoo {yahoo_sym} failed: {e}")
                return None

        # 并发抓取；10 个并发足够，避免触发 Yahoo 限流
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fetch, item) for item in targets.items()]
            for future in as_completed(futures):
                result = future.result()
                if not result:
                    continue
                yahoo_sym, category, data = result
                self.reference_prices[yahoo_sym] = data
                db_rows.append((yahoo_sym, ts, data['price'], category))

        self.db.execute_many(
            """
            INSERT INTO reference_prices (source, symbol, timestamp, price, category)
            VALUES ('yahoo', ?, ?, ?, ?)
            ON CONFLICT(source, symbol, timestamp) DO UPDATE SET
                price = excluded.price
            """,
            db_rows
        )
        logger.info(f"Reference prices updated: {len(db_rows)}/{len(targets)} symbols")

    def get_reference_prices(self) -> Dict[str, Dict[str, Any]]:
        return self.reference_prices

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
            'reference_prices': len(self.reference_prices),
            'peg_assets': len(self.peg_discovery.registry),
        }
