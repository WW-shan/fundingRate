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
        self.futures_volume: Dict[str, Dict[str, float]] = {}  # {exchange: {symbol: 24h quote volume}}
        self.stablecoin_data: Dict[str, Dict[str, Any]] = {}  # {symbol: {exchange: {...}}}
        self.reference_prices: Dict[str, Dict[str, Any]] = {}  # {yahoo_symbol: {...}}
        from config.peg_assets import PegAssetDiscovery
        self.peg_discovery = PegAssetDiscovery(config_manager)
        self._init_exchanges()

    # ---------- 初始化 ----------

    @staticmethod
    def _ticker_quote_volume(ticker: Dict[str, Any]) -> float:
        """ticker 的 24h 成交额（报价币种，通常为 USDT）"""
        qv = ticker.get("quoteVolume")
        try:
            qv_f = float(qv) if qv is not None else 0.0
        except (TypeError, ValueError):
            qv_f = 0.0
        if qv_f <= 0:
            bv = ticker.get("baseVolume")
            px = ticker.get("last") or ticker.get("bid") or ticker.get("ask")
            if bv is not None and px is not None:
                try:
                    qv_f = float(bv) * float(px)
                except (TypeError, ValueError):
                    qv_f = 0.0
        return max(0.0, qv_f)

    def get_futures_volume(self, exchange: str, symbol: str) -> float:
        """获取某交易所某永续的 24h 成交额（USDT）"""
        return float((self.futures_volume.get(exchange) or {}).get(symbol) or 0)

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
        if not enabled:
            enabled = ['binance', 'okx', 'bybit', 'gate', 'bitget']

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
        # 清理已禁用交易所的残留快照，避免面板继续展示旧数据
        active = set(new_exchanges)
        for sym, exchanges in list(self.market_data.items()):
            for ex in list(exchanges):
                if ex not in active:
                    del exchanges[ex]
            if not exchanges:
                del self.market_data[sym]
        for ex in list(self.futures_volume):
            if ex not in active:
                del self.futures_volume[ex]
        for sym, exchanges in list(self.stablecoin_data.items()):
            for ex in list(exchanges):
                if ex not in active:
                    del exchanges[ex]
            if not exchanges:
                del self.stablecoin_data[sym]
        logger.info(f"Active exchanges: {list(self.exchanges.keys())}")

    def reload_exchanges(self):
        """重新加载交易所连接（热更新）"""
        logger.info("Reloading exchange connections...")
        self._init_exchanges()
        # 让锚定资产注册表按新的交易所集合重建
        self.peg_discovery.registry = {}
        self.peg_discovery.by_underlying = {}
        self.peg_discovery.last_refresh = 0
        self.peg_discovery._shrink_streak = 0

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
            interval = self.config.get_int('global', 'funding_refresh_interval', 300)
            try:
                self._collect_funding_rates()
            except Exception as e:
                logger.error(f"Funding collection error: {e}")
            time.sleep(interval)

    def _collect_funding_rates(self):
        """批量采集全市场资金费率（每家 1 次 API 调用）"""
        for name, adapter in self.exchanges.items():
            if self.exchanges.get(name) is not adapter:
                continue
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
                for exchanges in list(self.market_data.values()):
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
                    try:
                        rate = float(rate)
                    except (TypeError, ValueError):
                        continue

                    iv_data = intervals.get(symbol) or {}
                    interval_ms = (adapter._parse_funding_interval(data)
                                   or adapter._parse_funding_interval(iv_data)
                                   or adapter.default_funding_interval_ms)
                    next_ts = data.get('fundingTimestamp') or iv_data.get('fundingTimestamp')

                    entry = {
                        'funding_rate': rate,
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
            interval = self.config.get_int('global', 'price_refresh_interval', 30)
            try:
                self._collect_prices()
            except Exception as e:
                logger.error(f"Price collection error: {e}")
            time.sleep(interval)

    def _collect_prices(self):
        """批量采集现货+永续价格（每家各 1 次批量调用）"""
        ts = int(time.time() * 1000)
        for name, adapter in self.exchanges.items():
            if self.exchanges.get(name) is not adapter:
                continue
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

                # 先记录所有永续的 24h 成交额（即使没有现货对，也可用于资金费率流动性过滤）
                new_futures_volume = {}
                for sym, ticker in (futures_tickers or {}).items():
                    if sym.endswith(':USDT'):
                        canonical = adapter.canonical_symbol(sym)
                        new_futures_volume[canonical] = self._ticker_quote_volume(ticker)
                if new_futures_volume:
                    self.futures_volume[name] = new_futures_volume

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
                price_fields = ('spot_bid', 'spot_ask', 'spot_price', 'spot_quote_volume',
                                'futures_bid', 'futures_ask', 'futures_price',
                                'futures_quote_volume', 'timestamp')
                for exchanges in list(self.market_data.values()):
                    entry = exchanges.get(name)
                    if entry:
                        for key in price_fields:
                            entry.pop(key, None)
                stale_ms = self.config.get_int('global', 'price_stale_threshold', 600) * 1000
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

                    spot_price = st.get('last') or st.get('bid') or st.get('ask')
                    futures_price = ft.get('last') or ft.get('bid') or ft.get('ask')
                    try:
                        spot_price = float(spot_price)
                        futures_price = float(futures_price)
                    except (TypeError, ValueError):
                        continue
                    if spot_price <= 0 or futures_price <= 0:
                        continue

                    entry = {
                        'spot_bid': st.get('bid'),
                        'spot_ask': st.get('ask'),
                        'spot_price': spot_price,
                        'spot_quote_volume': self._ticker_quote_volume(st),
                        'futures_bid': ft.get('bid'),
                        'futures_ask': ft.get('ask'),
                        'futures_price': futures_price,
                        'futures_quote_volume': self._ticker_quote_volume(ft),
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
            interval = self.config.get_int('global', 'stablecoin_refresh_interval', 60)
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
        stale_ms = self.config.get_int('global', 'price_stale_threshold', 600) * 1000
        collected = 0
        db_rows = []
        new_data = {}
        wanted = {f'{sym}/USDT': sym for sym in symbols}
        for name, adapter in self.exchanges.items():
            if self.exchanges.get(name) is not adapter:
                continue
            try:
                tickers = adapter.exchange.fetch_tickers(params={'type': 'spot'})
            except Exception as e:
                logger.debug(f"{name} peg tickers failed: {e}")
                continue

            # 单个交易所处理失败不影响其他交易所
            try:
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
                    raw_price = ticker.get('last') or ticker.get('bid') or ticker.get('ask')
                    if stale:
                        # 低流动性但仍活跃的市场可能没有最新成交，使用有效买一/卖一中间价
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
                        try:
                            ask_f = float(ticker.get('ask') or 0)
                            bid_f = float(ticker.get('bid') or 0)
                        except (TypeError, ValueError):
                            ask_f = bid_f = 0.0
                        bid = (1.0 / ask_f) if ask_f > 0 else None
                        ask = (1.0 / bid_f) if bid_f > 0 else None
                    else:
                        price = raw_price
                        bid = ticker.get('bid')
                        ask = ticker.get('ask')
                    # 快照时间统一用采集时间，避免低流动性标的的旧 ticker 时间戳
                    # 导致同类产品比价时被误判为过期而漏掉
                    new_data.setdefault(sym, {})[name] = {
                        'price': price,
                        'bid': bid,
                        'ask': ask,
                        'timestamp': ts,
                    }
                    db_rows.append((name, sym, ts, price, bid, ask))
                    collected += 1
            except Exception as e:
                logger.error(f"{name} peg ticker processing failed: {e}")
                continue

        if not new_data:
            logger.warning("Peg asset collection returned no quotes; keeping previous snapshot")
            return
        # 防止单所临时失败导致快照大幅缩水；覆盖率过低时保留上一轮完整快照
        coverage = len(new_data) / max(1, len(symbols))
        if self.stablecoin_data and coverage < 0.9:
            logger.warning(
                f"Peg asset collection incomplete ({len(new_data)}/{len(symbols)} symbols, "
                f"coverage={coverage:.0%}); keeping previous snapshot"
            )
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
            interval = self.config.get_int('global', 'reference_refresh_interval', 300)
            try:
                self._collect_reference_prices()
            except Exception as e:
                logger.error(f"Reference price collection error: {e}")
            time.sleep(interval)

    @staticmethod
    def _parse_iso_epoch(value):
        from datetime import datetime, timezone
        if not value:
            return None
        try:
            text = str(value).strip()
            if text.endswith('Z'):
                text = text[:-1] + '+00:00'
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_gold_api_payload(payload, ts):
        try:
            price = float(payload.get('price'))
        except (AttributeError, TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return {
            'price': price,
            'timestamp': ts,
            'market_time': DataCollector._parse_iso_epoch(payload.get('updatedAt')),
            'previous_close': None,
            'source': 'gold_api',
            'instrument': 'XAU/USD spot',
        }

    @staticmethod
    def _parse_swissquote_payload(payload, ts):
        if not isinstance(payload, list):
            return None
        entry = next((x for x in payload
                      if isinstance(x, dict) and x.get('spreadProfilePrices')), None)
        if not entry:
            return None
        quotes = [q for q in entry.get('spreadProfilePrices') or [] if isinstance(q, dict)]
        if not quotes:
            return None
        quote = next((q for q in quotes if q.get('spreadProfile') == 'premium'), quotes[0])
        try:
            bid = float(quote.get('bid'))
            ask = float(quote.get('ask'))
        except (TypeError, ValueError):
            return None
        if bid <= 0 or ask <= 0 or ask < bid:
            return None
        raw_ts = entry.get('ts')
        try:
            market_time = int(float(raw_ts) / 1000) if raw_ts else None
        except (TypeError, ValueError):
            market_time = None
        return {
            'price': (bid + ask) / 2,
            'timestamp': ts,
            'market_time': market_time,
            'previous_close': None,
            'source': 'swissquote',
            'instrument': 'XAU/USD spot',
        }

    @staticmethod
    def _fetch_yahoo_reference(ref_symbol, category, proxies, ts,
                               source='yahoo', instrument=None):
        import requests
        try:
            r = requests.get(
                f'https://query1.finance.yahoo.com/v8/finance/chart/{ref_symbol}?interval=1m&range=1d',
                proxies=proxies, timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            result = r.json()['chart']['result'][0]
            price = result['meta'].get('regularMarketPrice')
            if not price:
                return None
            return {
                'price': float(price),
                'category': category,
                'timestamp': ts,
                'market_time': result['meta'].get('regularMarketTime'),
                'previous_close': result['meta'].get('previousClose'),
                'source': source,
                'instrument': instrument or ref_symbol,
            }
        except Exception as e:
            logger.debug(f"Yahoo {ref_symbol} failed: {e}")
            return None

    def _fetch_spot_gold(self, proxies, ts):
        import requests
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            r = requests.get(
                'https://api.gold-api.com/price/XAU',
                proxies=proxies, timeout=15, headers=headers
            )
            r.raise_for_status()
            data = self._parse_gold_api_payload(r.json(), ts)
            if data:
                return data
        except Exception as e:
            logger.debug(f"gold-api XAU failed: {e}")

        try:
            r = requests.get(
                'https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD',
                proxies=proxies, timeout=15, headers=headers
            )
            r.raise_for_status()
            data = self._parse_swissquote_payload(r.json(), ts)
            if data:
                return data
        except Exception as e:
            logger.debug(f"Swissquote XAU failed: {e}")

        logger.warning("Spot gold sources failed; falling back to Yahoo GC=F futures")
        return self._fetch_yahoo_reference(
            'GC=F', 'commodity', proxies, ts,
            source='yahoo_futures', instrument='GC=F futures fallback'
        )

    def _collect_reference_prices(self):
        """Collect spot gold, EUR/USD and commodity ETF references."""
        from config import peg_assets
        from concurrent.futures import ThreadPoolExecutor, as_completed

        proxy = os.getenv('PROXY_URL', '') or self.config.get('global', 'reference_proxy', '')
        proxies = {'http': proxy, 'https': proxy} if proxy else None

        if not self.peg_discovery.registry:
            self.peg_discovery.refresh(self.exchanges, force=True)

        targets = {
            peg_assets.GOLD_SPOT_SYMBOL: 'commodity',
            peg_assets.YAHOO_SYMBOLS['eurusd']: 'eur_stable',
        }
        active_symbols = set(self.stablecoin_data)
        for sym, entry in self.peg_discovery.registry.items():
            ref = entry.get('reference_symbol')
            if sym in active_symbols and ref and entry.get('category') in ('commodity', 'eur_stable'):
                targets[ref] = entry['category']

        ts = int(time.time() * 1000)
        db_rows = []

        def fetch(item):
            ref_symbol, category = item
            if ref_symbol == peg_assets.GOLD_SPOT_SYMBOL:
                data = self._fetch_spot_gold(proxies, ts)
            else:
                data = self._fetch_yahoo_reference(ref_symbol, category, proxies, ts)
            if not data:
                return None
            return ref_symbol, category, data

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(fetch, item) for item in targets.items()]
            for future in as_completed(futures):
                result = future.result()
                if not result:
                    continue
                ref_symbol, category, data = result
                self.reference_prices[ref_symbol] = data
                db_rows.append((data.get('source', 'yahoo'), ref_symbol, ts,
                                data['price'], category))

        self.db.execute_many(
            """
            INSERT INTO reference_prices (source, symbol, timestamp, price, category)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, symbol, timestamp) DO UPDATE SET
                price = excluded.price
            """,
            db_rows
        )
        logger.info(f"Reference prices updated: {len(db_rows)}/{len(targets)} symbols")

    def get_reference_prices(self) -> Dict[str, Dict[str, Any]]:
        # 返回快照，避免监控线程遍历时与采集线程写入互相干扰
        return {sym: dict(entry) for sym, entry in list(self.reference_prices.items())}

    # ---------- 供监控模块使用 ----------

    def get_market_data(self) -> Dict[str, Dict[str, Any]]:
        # 深一层拷贝：外层与每个交易所 entry 都复制，避免采集线程并发修改
        return {
            sym: {ex: dict(entry) for ex, entry in list(exchanges.items())}
            for sym, exchanges in list(self.market_data.items())
        }

    def get_stablecoin_data(self) -> Dict[str, Dict[str, Any]]:
        return {
            sym: {ex: dict(entry) for ex, entry in list(exchanges.items())}
            for sym, exchanges in list(self.stablecoin_data.items())
        }

    def get_status(self) -> Dict[str, Any]:
        return {
            'running': self.running,
            'exchanges': list(self.exchanges.keys()),
            'symbols_tracked': len(self.market_data),
            'stablecoins_tracked': len(self.stablecoin_data),
            'reference_prices': len(self.reference_prices),
            'peg_assets': len(self.peg_discovery.registry),
        }
