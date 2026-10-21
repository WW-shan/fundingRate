"""
OKX 适配器（监控版）
"""
import threading

import ccxt
from loguru import logger

from .base_exchange import BaseExchange


class OKXAdapter(BaseExchange):
    # OKX fundingRate 是下期预测费率
    funding_rate_semantics = 'upcoming'

    def _init_exchange(self):
        self.exchange = ccxt.okx(self._ccxt_config())
        self._markets_lock = threading.Lock()
        self._install_safe_load_markets()

    def _install_safe_load_markets(self):
        """Filter OKX pre-open instruments with an empty instId.

        ccxt builds markets_by_id with a None key for those instruments and
        then fails while sorting it.  Loading a cleaned market list once avoids
        intermittent fetch_tickers/fetch_funding_rates failures.
        """
        exchange = self.exchange

        def safe_load_markets(reload=False, params=None):
            if isinstance(reload, dict):
                params = reload
                reload = False
            params = params or {}
            with self._markets_lock:
                if exchange.markets and not reload:
                    return exchange.markets
                markets = exchange.fetch_markets(params)
                clean = [m for m in markets if m.get('id') and m.get('symbol')]
                dropped = len(markets) - len(clean)
                if dropped:
                    logger.warning(f"OKX filtered {dropped} malformed market(s) before loading")
                exchange.set_markets(clean)
                return exchange.markets

        exchange.load_markets = safe_load_markets
