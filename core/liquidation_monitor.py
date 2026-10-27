"""跨交易所爆仓量监控。

五家交易所统一归一化为 USD 名义金额，并按币种和方向聚合：
  - Binance / OKX / Bybit / Bitget: 公开 WebSocket 强平推送
  - Gate: 公开 REST 强平订单接口（其强平 WebSocket 需要账户鉴权）

检测方式不是依赖单笔“大户”，而是比较最近一段滚动窗口的爆仓金额与
过去若干分钟的正常水平。这样可以识别一段行情里持续出现的大量强平。
"""

import asyncio
import json
import os
import statistics
import threading
import time
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import websockets
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class LiquidationMonitor:
    """五家交易所爆仓量采集、聚合、异动识别与告警。"""

    EXCHANGES = ("binance", "okx", "bybit", "gate", "bitget")
    BINANCE_LIQUIDATION_STREAM = "!forceOrder@arr"
    # bookTicker can remain available through proxies that silently discard
    # lower-volume or derived futures streams. Mark price provides a frequent
    # canary on the same data path as forceOrder.
    BINANCE_HEALTH_STREAM = "btcusdt@markPrice@1s"
    WS_ENDPOINTS = {
        "binance": "wss://fstream.binance.com/market/ws",
        "okx": "wss://ws.okx.com:443/ws/v5/public",
        "bybit": "wss://stream.bybit.com/v5/public/linear",
        "bitget": "wss://ws.bitget.com/v3/ws/public",
    }

    def __init__(self, config_manager, db_manager, data_collector=None):
        self.config = config_manager
        self.db = db_manager
        self.data_collector = data_collector
        self.running = False
        self.alert_callbacks = []
        self.last_scan: Dict[str, Any] = {}

        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._threads: List[threading.Thread] = []
        self._ws_handles: Dict[str, Tuple[Any, Any]] = {}
        self._events: Dict[str, deque] = defaultdict(deque)
        self._recent_event_keys = deque()
        self._recent_event_key_set = set()
        self._price_samples: Dict[str, deque] = defaultdict(deque)
        self._minute_buckets: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
        self._minute_totals: Dict[Tuple[int, str, str], float] = {}
        self._connections: Dict[str, Dict[str, Any]] = {
            name: {
                "connected": False,
                "last_message": None,
                "last_event": None,
                "last_error": "",
                "events": 0,
            }
            for name in self.EXCHANGES
        }
        self._okx_instruments: Dict[str, Dict[str, Any]] = {}
        self._gate_multipliers: Dict[str, float] = {}
        self._gate_contracts_loaded_at = 0.0
        self._started_at = 0.0

    def register_alert_callback(self, callback):
        self.alert_callbacks.append(callback)

    def start(self):
        if not self.config.get_bool("alerts", "liquidation_enabled", True):
            logger.info("Liquidation monitor disabled by config")
            return
        if self.running:
            return

        self.running = True
        self._stop_event.clear()
        self._started_at = time.time()
        self._threads = []
        with self._lock:
            self._events.clear()
            self._recent_event_keys.clear()
            self._recent_event_key_set.clear()
            self._minute_buckets.clear()
            self._minute_totals.clear()
            self._price_samples.clear()
            for state in self._connections.values():
                state["connected"] = False
                state["last_message"] = None
                state["last_event"] = None
                state["last_error"] = ""
                state["events"] = 0
        enabled = self._enabled_exchanges()
        for name in enabled:
            target = self._gate_loop if name == "gate" else self._websocket_thread
            thread_args = () if name == "gate" else (name,)
            thread = threading.Thread(
                target=target,
                args=thread_args,
                name=f"liquidation-{name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        thread = threading.Thread(
            target=self._monitor_loop,
            name="liquidation-aggregator",
            daemon=True,
        )
        thread.start()
        self._threads.append(thread)
        logger.info(f"Liquidation monitor started for: {enabled}")

    def stop(self):
        self.running = False
        self._stop_event.set()
        with self._lock:
            for state in self._connections.values():
                state["connected"] = False
            ws_handles = list(self._ws_handles.values())
        for loop, ws in ws_handles:
            try:
                loop.call_soon_threadsafe(
                    lambda websocket=ws: asyncio.create_task(websocket.close())
                )
            except Exception:
                pass
        for thread in self._threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=0.5)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        try:
            self._flush_buckets()
        except Exception as e:
            logger.warning(f"Final liquidation flush failed: {e}")
        logger.info("Liquidation monitor stopped")

    def _enabled_exchanges(self) -> List[str]:
        enabled = self.config.get(
            "global",
            "enabled_exchanges",
            ["binance", "okx", "bybit", "gate", "bitget"],
        )
        if isinstance(enabled, str):
            enabled = [x.strip().lower() for x in enabled.split(",") if x.strip()]
        if not isinstance(enabled, (list, tuple, set)):
            enabled = list(self.EXCHANGES)
        wanted = {str(x).lower() for x in enabled}
        return [name for name in self.EXCHANGES if name in wanted]

    def required_active_sources(self) -> int:
        configured = max(
            1,
            self.config.get_int("alerts", "liquidation_min_sources", 2),
        )
        enabled_count = max(1, len(self._enabled_exchanges()))
        return min(configured, enabled_count)

    def _proxy(self, exchange: str) -> Optional[str]:
        env_key = f"{exchange.upper()}_PROXY"
        return os.getenv(env_key) or os.getenv("PROXY_URL") or None

    def _requests_proxies(self, exchange: str) -> Optional[Dict[str, str]]:
        proxy = self._proxy(exchange)
        return {"http": proxy, "https": proxy} if proxy else None

    @staticmethod
    def _dedupe_proxies(proxies: Iterable[Optional[str]]) -> List[Optional[str]]:
        result = []
        seen = set()
        for proxy in proxies:
            key = proxy or ""
            if key not in seen:
                seen.add(key)
                result.append(proxy)
        return result or [None]

    def _websocket_proxy_candidates(self, exchange: str) -> List[Optional[str]]:
        specific = os.getenv(f"{exchange.upper()}_PROXY") or None
        generic = os.getenv("PROXY_URL") or None
        if exchange == "binance":
            # The generic proxy may accept Binance handshakes while silently
            # dropping market-data frames. Prefer a Binance-specific proxy or
            # a direct connection, then use the generic proxy as fallback.
            return self._dedupe_proxies([specific, None, generic])
        return self._dedupe_proxies([specific or generic])

    @staticmethod
    def _new_gate_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.3,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=1,
            pool_maxsize=1,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "Accept": "application/json",
                "Connection": "keep-alive",
                "User-Agent": "funding-rate-monitor/2.0",
            }
        )
        return session

    def _mark_connected(self, exchange: str, connected: bool):
        with self._lock:
            if connected and self._stop_event.is_set():
                connected = False
            self._connections[exchange]["connected"] = connected
            if connected:
                self._connections[exchange]["last_message"] = None
                self._connections[exchange]["last_error"] = ""

    def _mark_error(self, exchange: str, error: Any):
        with self._lock:
            self._connections[exchange]["connected"] = False
            self._connections[exchange]["last_error"] = str(error)[:300]

    @staticmethod
    def _is_source_active(
        state: Dict[str, Any], now: float, stale_seconds: int
    ) -> bool:
        last_message = state.get("last_message")
        return bool(
            state.get("connected")
            and last_message
            and now - float(last_message) <= stale_seconds
        )

    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        enabled_exchanges = self._enabled_exchanges()
        stale_seconds = max(
            30,
            self.config.get_int(
                "alerts", "liquidation_source_stale_seconds", 120
            ),
        )
        with self._lock:
            connections = {
                name: {
                    **state,
                    "active": self._is_source_active(state, now, stale_seconds),
                    "last_message_age_seconds": (
                        round(now - state["last_message"], 1)
                        if state.get("last_message")
                        else None
                    ),
                }
                for name, state in self._connections.items()
            }
            active = [
                name for name, state in connections.items() if state.get("active")
            ]
            event_count = sum(len(events) for events in self._events.values())
            symbol_count = len(self._events)
            scan = dict(self.last_scan)

        return {
            "running": self.running,
            "connections": connections,
            "active_exchanges": active,
            "active_exchange_count": len(active),
            "enabled_exchanges": enabled_exchanges,
            "enabled_exchange_count": len(enabled_exchanges),
            "required_active_sources": self.required_active_sources(),
            "symbols_tracked": symbol_count,
            "events_in_memory": event_count,
            "last_scan": scan,
        }

    # ---------- WebSocket sessions ----------

    def _websocket_thread(self, exchange: str):
        proxy_candidates = self._websocket_proxy_candidates(exchange)
        proxy_index = 0
        while self.running and not self._stop_event.is_set():
            proxy = proxy_candidates[proxy_index % len(proxy_candidates)]
            proxy_label = "direct" if proxy is None else "proxy"
            try:
                asyncio.run(self._websocket_session(exchange, proxy))
            except Exception as e:
                if self.running:
                    self._mark_error(exchange, e)
                    logger.warning(
                        f"{exchange} liquidation WebSocket error ({proxy_label}): {e}"
                    )
            proxy_index += 1
            if self.running:
                self._stop_event.wait(5)

    async def _wait_for_binance_data(self, ws, timeout: int = 10):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.running and not self._stop_event.is_set():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError(
                    "Binance mark-price health probe received no data; "
                    "the current route may be silently dropping futures streams"
                )
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Binance mark-price health probe received no data; "
                    "the current route may be silently dropping futures streams"
                ) from exc
            self._handle_raw_message("binance", raw)
            with self._lock:
                if self._connections["binance"]["last_message"] is not None:
                    return

    async def _websocket_session(
        self, exchange: str, proxy: Optional[str] = None
    ):
        async with websockets.connect(
            self.WS_ENDPOINTS[exchange],
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
            proxy=proxy,
        ) as ws:
            loop = asyncio.get_running_loop()
            with self._lock:
                self._ws_handles[exchange] = (loop, ws)
            self._mark_connected(exchange, True)
            logger.info(f"{exchange} liquidation WebSocket connected")
            heartbeat = None
            try:
                if exchange == "binance":
                    await ws.send(
                        json.dumps(
                            {
                                "method": "SUBSCRIBE",
                                "params": [
                                    self.BINANCE_LIQUIDATION_STREAM,
                                    self.BINANCE_HEALTH_STREAM,
                                ],
                                "id": 1,
                            }
                        )
                    )
                elif exchange == "okx":
                    self._prepare_okx()
                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "args": [
                                    {
                                        "channel": "liquidation-orders",
                                        "instType": "SWAP",
                                    }
                                ],
                            }
                        )
                    )
                elif exchange == "bybit":
                    symbols = await asyncio.to_thread(self._fetch_bybit_symbols)
                    for batch in self._chunks(symbols, 10):
                        await ws.send(
                            json.dumps(
                                {
                                    "op": "subscribe",
                                    "args": [
                                        f"allLiquidation.{symbol}" for symbol in batch
                                    ],
                                }
                            )
                        )
                        await asyncio.sleep(0.05)
                    logger.info(
                        f"bybit subscribed to {len(symbols)} liquidation symbols"
                    )
                elif exchange == "bitget":
                    await ws.send(
                        json.dumps(
                            {
                                "op": "subscribe",
                                "args": [
                                    {
                                        "instType": "usdt-futures",
                                        "topic": "liquidation",
                                    }
                                ],
                            }
                        )
                    )

                if exchange == "binance":
                    await self._wait_for_binance_data(ws)

                heartbeat = asyncio.create_task(self._heartbeat(exchange, ws))
                while self.running and not self._stop_event.is_set():
                    raw = await ws.recv()
                    self._handle_raw_message(exchange, raw)
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    try:
                        await heartbeat
                    except BaseException:
                        pass
                with self._lock:
                    self._ws_handles.pop(exchange, None)
                self._mark_connected(exchange, False)

    async def _heartbeat(self, exchange: str, ws):
        while self.running and not self._stop_event.is_set():
            await asyncio.sleep(20)
            try:
                if exchange == "binance":
                    with self._lock:
                        last_message = self._connections["binance"].get(
                            "last_message"
                        )
                    if not last_message or time.time() - last_message > 35:
                        await ws.close()
                        return
                elif exchange == "okx":
                    await ws.send("ping")
                elif exchange == "bybit":
                    await ws.send(json.dumps({"op": "ping"}))
                elif exchange == "bitget":
                    await ws.send("ping")
            except Exception:
                return

    @staticmethod
    def _chunks(values: List[str], size: int) -> Iterable[List[str]]:
        for index in range(0, len(values), size):
            yield values[index : index + size]

    def _fetch_bybit_symbols(self) -> List[str]:
        symbols = []
        cursor = ""
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            response = requests.get(
                "https://api.bybit.com/v5/market/instruments-info",
                params=params,
                proxies=self._requests_proxies("bybit"),
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode") != 0:
                raise RuntimeError(payload.get("retMsg") or "Bybit instruments failed")
            result = payload.get("result") or {}
            for row in result.get("list", []):
                if (
                    row.get("quoteCoin") == "USDT"
                    and row.get("status") == "Trading"
                    and row.get("contractType") == "LinearPerpetual"
                ):
                    symbols.append(row["symbol"])
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return symbols

    def _prepare_okx(self):
        response = requests.get(
            "https://www.okx.com/api/v5/public/instruments",
            params={"instType": "SWAP"},
            proxies=self._requests_proxies("okx"),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "0":
            raise RuntimeError(payload.get("msg") or "OKX instruments failed")
        self._okx_instruments = {
            row["instId"]: row
            for row in payload.get("data", [])
            if row.get("instId", "").endswith("-USDT-SWAP")
        }

    # ---------- message parsers ----------

    @staticmethod
    def _is_subscription_ack(exchange: str, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if exchange == "binance":
            return "id" in payload and "result" in payload and "e" not in payload
        if exchange == "okx":
            return payload.get("event") in ("subscribe", "unsubscribe")
        if exchange == "bybit":
            return (
                payload.get("success") is True
                and payload.get("op") in ("subscribe", "unsubscribe")
            )
        if exchange == "bitget":
            return payload.get("event") in ("subscribe", "unsubscribe")
        return False

    def _handle_raw_message(self, exchange: str, raw: Any):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if raw in ("pong", '{"op":"pong"}', '{"event":"pong"}'):
            with self._lock:
                self._connections[exchange]["last_message"] = time.time()
            return
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if self._is_subscription_ack(exchange, payload):
            return

        with self._lock:
            self._connections[exchange]["last_message"] = time.time()

        if exchange == "binance":
            self._parse_binance(payload)
        elif exchange == "okx":
            self._parse_okx(payload)
        elif exchange == "bybit":
            self._parse_bybit(payload)
        elif exchange == "bitget":
            self._parse_bitget(payload)

    def _parse_binance(self, payload: Any):
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        rows = payload if isinstance(payload, list) else [payload]
        for item in rows:
            if not isinstance(item, dict) or item.get("e") != "forceOrder":
                continue
            order = item.get("o")
            if not isinstance(order, dict):
                continue
            raw_symbol = str(order.get("s") or "").upper()
            if not raw_symbol.endswith("USDT"):
                continue
            symbol = self._base_symbol(raw_symbol)
            if not symbol:
                continue
            qty = self._float(order.get("z") or order.get("q"))
            price = self._float(order.get("ap") or order.get("p"))
            side_raw = str(order.get("S", "")).upper()
            if side_raw not in ("BUY", "SELL"):
                continue
            side = "long" if side_raw == "SELL" else "short"
            self._record_event(
                "binance",
                symbol,
                side,
                qty * price,
                price,
                self._int(item.get("E") or order.get("T")),
            )

    def _parse_okx(self, payload: Any):
        if not isinstance(payload, dict):
            return
        if payload.get("event") == "error":
            raise RuntimeError(payload.get("msg") or "OKX subscription error")
        if payload.get("event") == "notice":
            return
        for group in payload.get("data") or []:
            if not isinstance(group, dict):
                continue
            inst_id = str(group.get("instId") or "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            symbol = self._base_symbol(inst_id)
            meta = self._okx_instruments.get(inst_id)
            if not meta:
                continue
            ct_val = self._float(meta.get("ctVal"))
            if ct_val <= 0:
                continue
            ct_type = meta.get("ctType") or "linear"
            for detail in group.get("details") or []:
                if not isinstance(detail, dict):
                    continue
                qty = self._float(detail.get("sz"))
                price = self._float(detail.get("bkPx"))
                if ct_type == "linear":
                    usd = qty * ct_val * price
                else:
                    usd = qty * ct_val
                side_raw = str(detail.get("posSide") or "").lower()
                if side_raw in ("long", "short"):
                    side = side_raw
                else:
                    order_side = str(detail.get("side", "")).lower()
                    if order_side not in ("buy", "sell"):
                        continue
                    side = "long" if order_side == "sell" else "short"
                self._record_event(
                    "okx", symbol, side, usd, price, self._int(detail.get("ts"))
                )

    def _parse_bybit(self, payload: Any):
        if not isinstance(payload, dict):
            return
        if payload.get("success") is False:
            raise RuntimeError(payload.get("ret_msg") or "Bybit subscription error")
        topic = payload.get("topic") or ""
        if not topic.startswith("allLiquidation."):
            return
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            symbol = self._base_symbol(row.get("s"))
            qty = self._float(row.get("v"))
            price = self._float(row.get("p"))
            # Bybit: Buy update = long position liquidated; Sell = short liquidated.
            side_raw = str(row.get("S", "")).lower()
            if side_raw not in ("buy", "sell"):
                continue
            side = "long" if side_raw == "buy" else "short"
            self._record_event(
                "bybit",
                symbol,
                side,
                qty * price,
                price,
                self._int(row.get("T") or payload.get("ts")),
            )

    def _parse_bitget(self, payload: Any):
        if not isinstance(payload, dict):
            return
        if payload.get("event") == "error":
            raise RuntimeError(payload.get("msg") or "Bitget subscription error")
        arg = payload.get("arg") or {}
        if arg.get("topic") != "liquidation":
            return
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            symbol = self._base_symbol(row.get("symbol"))
            price = self._float(row.get("price"))
            usd = self._float(row.get("amount"))
            # Bitget docs: buy = long liquidation, sell = short liquidation.
            side_raw = str(row.get("side", "")).lower()
            if side_raw not in ("buy", "sell"):
                continue
            side = "long" if side_raw == "buy" else "short"
            self._record_event(
                "bitget",
                symbol,
                side,
                usd,
                price,
                self._int(row.get("ts") or payload.get("ts")),
            )

    # ---------- Gate REST polling ----------

    @staticmethod
    def _gate_query_range(now_second: int, previous_query_second: Optional[int]):
        start = (
            now_second - 2
            if previous_query_second is None
            else previous_query_second - 2
        )
        return max(0, now_second - 3600, start), now_second

    @staticmethod
    def _fetch_gate_liquidation_rows(
        session,
        from_second: int,
        to_second: int,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        url = "https://api.gateio.ws/api/v4/futures/usdt/liq_orders"

        def fetch(start: int, end: int) -> List[Dict[str, Any]]:
            response = session.get(
                url,
                params={"from": start, "to": end, "limit": limit},
                timeout=(5, 10),
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise RuntimeError("Gate liquidation response is not a list")
            return [row for row in rows if isinstance(row, dict)]

        def collect(start: int, end: int, rows: List[Dict[str, Any]]):
            if len(rows) < limit:
                return rows
            if start >= end:
                logger.warning(
                    f"Gate liquidation query reached the {limit}-row limit "
                    f"at second {start}; that second may be incomplete"
                )
                return rows

            midpoint = (start + end) // 2
            return collect(start, midpoint, fetch(start, midpoint)) + collect(
                midpoint + 1, end, fetch(midpoint + 1, end)
            )

        rows = fetch(from_second, to_second)
        if len(rows) >= limit and from_second < to_second:
            logger.warning(
                f"Gate liquidation query reached the {limit}-row limit; "
                "retrying with smaller time ranges"
            )
        return collect(from_second, to_second, rows)

    def _gate_loop(self):
        last_query_second = None
        seen = set()
        session = self._new_gate_session()
        gate_proxies = self._requests_proxies("gate")
        if gate_proxies:
            session.proxies.update(gate_proxies)
            session.trust_env = False
        poll_interval = max(
            1,
            self.config.get_int(
                "alerts", "liquidation_gate_poll_seconds", 2
            ),
        )
        consecutive_errors = 0
        try:
            while self.running and not self._stop_event.is_set():
                wait_seconds = poll_interval
                try:
                    metadata_age = time.time() - self._gate_contracts_loaded_at
                    if metadata_age > 3600 or (
                        not self._gate_multipliers and metadata_age > 60
                    ):
                        self._load_gate_contracts(session)
                    query_to = int(time.time())
                    query_from, query_to = self._gate_query_range(
                        query_to, last_query_second
                    )
                    rows = self._fetch_gate_liquidation_rows(
                        session, query_from, query_to
                    )
                    if self._stop_event.is_set():
                        break
                    consecutive_errors = 0
                    now = time.time()
                    with self._lock:
                        self._connections["gate"]["connected"] = True
                        self._connections["gate"]["last_message"] = now
                        self._connections["gate"]["last_error"] = ""
                    rows = [
                        row
                        for row in rows
                        if isinstance(row, dict)
                        and query_from
                        <= self._int(row.get("time"))
                        <= query_to
                    ]
                    if not rows:
                        last_query_second = query_to
                        self._stop_event.wait(wait_seconds)
                        continue

                    for row in rows:
                        event_second = self._int(row.get("time"))
                        key = (
                            row.get("contract"),
                            event_second,
                            row.get("size"),
                            row.get("order_size"),
                            row.get("fill_price"),
                            row.get("order_price"),
                            row.get("left"),
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        self._parse_gate_row(row)
                    last_query_second = query_to
                    if len(seen) > 10000:
                        seen = {
                            key for key in seen if key[1] >= query_to - 600
                        }
                except Exception as e:
                    consecutive_errors += 1
                    self._mark_error("gate", e)
                    wait_seconds = min(
                        30, max(poll_interval, 2 ** min(consecutive_errors, 4))
                    )
                    logger.warning(
                        "gate liquidation polling error "
                        f"(retry in {wait_seconds}s): {e}"
                    )
                self._stop_event.wait(wait_seconds)
        finally:
            session.close()

    def _parse_gate_row(self, row: Dict[str, Any]) -> bool:
        if not isinstance(row, dict):
            return False
        contract = str(row.get("contract") or "").upper()
        order_size = self._float(row.get("order_size"))
        price = self._float(row.get("fill_price") or row.get("order_price"))
        multiplier = self._gate_multipliers.get(contract)
        event_second = self._int(row.get("time"))
        if not contract or not multiplier or order_size == 0 or price <= 0:
            return False
        usd = abs(order_size) * price * multiplier
        # Gate order_size is signed like a futures order: sells close longs;
        # buys close shorts. `size` is the position size, not executed qty.
        side = "long" if order_size < 0 else "short"
        self._record_event(
            "gate",
            self._base_symbol(contract),
            side,
            usd,
            price,
            event_second * 1000,
        )
        return True

    def _load_gate_contracts(self, session: Optional[requests.Session] = None):
        if session is None:
            response = requests.get(
                "https://api.gateio.ws/api/v4/futures/usdt/contracts",
                proxies=self._requests_proxies("gate"),
                timeout=20,
            )
        else:
            response = session.get(
                "https://api.gateio.ws/api/v4/futures/usdt/contracts",
                timeout=(5, 12),
            )
        response.raise_for_status()
        rows = response.json()
        multipliers = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            multiplier = self._float(row.get("quanto_multiplier"))
            if multiplier > 0:
                multipliers[row["name"]] = multiplier
        self._gate_multipliers = multipliers
        self._gate_contracts_loaded_at = time.time()

    # ---------- normalized event store ----------

    def _record_event(
        self,
        exchange: str,
        symbol: str,
        side: str,
        usd: float,
        price: float,
        timestamp_ms: int,
    ):
        if self._stop_event.is_set():
            return
        if not symbol or usd <= 0 or side not in ("long", "short"):
            return
        now_ms = int(time.time() * 1000)
        if timestamp_ms <= 0:
            timestamp_ms = now_ms
        timestamp_ms = min(timestamp_ms, now_ms + 60_000)
        event = {
            "timestamp": timestamp_ms,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "usd": float(usd),
            "price": float(price or 0),
        }
        event_key = (
            exchange,
            timestamp_ms,
            symbol,
            side,
            round(event["usd"], 8),
            round(event["price"], 12),
        )
        minute = timestamp_ms // 60_000 * 60_000
        bucket_key = (minute, exchange, symbol, side)
        with self._lock:
            if event_key in self._recent_event_key_set:
                return
            self._recent_event_key_set.add(event_key)
            self._recent_event_keys.append(event_key)
            while len(self._recent_event_keys) > 20_000:
                expired = self._recent_event_keys.popleft()
                self._recent_event_key_set.discard(expired)
            self._events[symbol].append(event)
            bucket = self._minute_buckets.setdefault(
                bucket_key,
                {
                    "usd": 0.0,
                    "count": 0,
                    "max_order_usd": 0.0,
                },
            )
            bucket["usd"] += event["usd"]
            bucket["count"] += 1
            bucket["max_order_usd"] = max(bucket["max_order_usd"], event["usd"])
            minute_total_key = (minute, symbol, side)
            self._minute_totals[minute_total_key] = (
                self._minute_totals.get(minute_total_key, 0.0) + event["usd"]
            )
            state = self._connections[exchange]
            state["last_event"] = now_ms
            state["events"] = int(state.get("events") or 0) + 1

    # ---------- aggregation / detection ----------

    def _monitor_loop(self):
        while self.running and not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception as e:
                logger.error(f"Liquidation scan error: {e}")
            interval = max(
                3, self.config.get_int("alerts", "liquidation_scan_interval", 10)
            )
            self._stop_event.wait(interval)

    def scan_once(self) -> Dict[str, Any]:
        now = time.time()
        now_ms = int(now * 1000)
        window_seconds = max(
            10, self.config.get_int("alerts", "liquidation_window_seconds", 60)
        )
        baseline_minutes = max(
            5, self.config.get_int("alerts", "liquidation_baseline_minutes", 30)
        )
        event_horizon_ms = int((window_seconds + 120) * 1000)
        baseline_horizon_ms = int(
            (window_seconds + baseline_minutes * 60 + 120) * 1000
        )
        current_start = now_ms - window_seconds * 1000

        with self._lock:
            event_cutoff = now_ms - event_horizon_ms
            for symbol in list(self._events):
                events = deque(
                    event
                    for event in self._events[symbol]
                    if event["timestamp"] >= event_cutoff
                )
                if events:
                    self._events[symbol] = events
                else:
                    del self._events[symbol]

            minute_total_cutoff = now_ms - baseline_horizon_ms
            for key in list(self._minute_totals):
                if key[0] < minute_total_cutoff:
                    del self._minute_totals[key]

            snapshots = {
                symbol: list(events) for symbol, events in self._events.items()
            }
            minute_totals = dict(self._minute_totals)
            stale_seconds = max(
                30,
                self.config.get_int(
                    "alerts", "liquidation_source_stale_seconds", 120
                ),
            )
            active_sources = [
                name
                for name, state in self._connections.items()
                if self._is_source_active(state, now, stale_seconds)
            ]

        self._capture_market_prices(snapshots.keys())
        min_alert_usd = max(
            0.0,
            self.config.get_float(
                "alerts",
                "liquidation_min_alert_usd",
                500_000,
            ),
        )
        ratio_threshold = max(
            1.2,
            self.config.get_float(
                "alerts",
                "liquidation_ratio_threshold",
                3.0,
            ),
        )
        large_order_usd = max(
            0.0,
            self.config.get_float(
                "alerts",
                "liquidation_large_order_usd",
                1_000_000,
            ),
        )
        min_sources = self.required_active_sources()
        alert_sides = self._alert_sides()

        ranking = []
        alerts = []
        for symbol, events in snapshots.items():
            for side in ("short", "long"):
                current = [
                    e
                    for e in events
                    if e["side"] == side and e["timestamp"] >= current_start
                ]
                if not current:
                    continue

                current_usd = sum(e["usd"] for e in current)
                max_order = max(e["usd"] for e in current)
                exchange_breakdown: Dict[str, float] = defaultdict(float)
                for event in current:
                    exchange_breakdown[event["exchange"]] += event["usd"]

                baseline_start_ms = max(
                    int(self._started_at * 1000),
                    current_start - baseline_minutes * 60_000,
                )
                baseline_values = []
                for offset in range(1, baseline_minutes + 1):
                    minute_end = (current_start // 60_000 - offset + 1) * 60_000
                    minute_start = minute_end - 60_000
                    if minute_start < baseline_start_ms:
                        continue
                    baseline_values.append(
                        minute_totals.get((minute_start, symbol, side), 0.0)
                    )
                baseline = (
                    sum(baseline_values) / len(baseline_values)
                    if baseline_values
                    else 0.0
                )
                effective_baseline = max(baseline, min_alert_usd / ratio_threshold)
                trigger_ratio = (
                    current_usd / effective_baseline if effective_baseline > 0 else 0.0
                )
                ratio = current_usd / baseline if baseline > 0 else None
                price = self._weighted_price(current)
                price_change = self._market_price_change(symbol, window_seconds)

                row = {
                    "symbol": symbol,
                    "side": side,
                    "side_label": "空单" if side == "short" else "多单",
                    "window_seconds": window_seconds,
                    "current_usd": round(current_usd, 2),
                    "baseline_usd": round(baseline, 2),
                    "ratio": round(ratio, 2) if ratio is not None else None,
                    "trigger_ratio": round(trigger_ratio, 2),
                    "event_count": len(current),
                    "max_order_usd": round(max_order, 2),
                    "exchange_breakdown": {
                        name: round(value, 2)
                        for name, value in sorted(
                            exchange_breakdown.items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )
                    },
                    "price": price,
                    "price_change_pct": price_change,
                    "timestamp": now_ms,
                }
                ranking.append(row)

                warmup_seconds = (
                    max(
                        0,
                        self.config.get_int(
                            "alerts",
                            "liquidation_warmup_minutes",
                            5,
                        ),
                    )
                    * 60
                )
                if now - self._started_at < warmup_seconds:
                    continue
                if side not in alert_sides:
                    continue
                if len(active_sources) < min_sources:
                    continue
                if current_usd < min_alert_usd:
                    continue

                severity_ratio = ratio if ratio is not None else trigger_ratio
                ratio_trigger = trigger_ratio >= ratio_threshold
                large_order_trigger = (
                    large_order_usd > 0
                    and max_order >= large_order_usd
                    and severity_ratio >= 1.5
                )
                if not (ratio_trigger or large_order_trigger):
                    continue

                critical_ratio = max(
                    ratio_threshold,
                    self.config.get_float(
                        "alerts",
                        "liquidation_critical_ratio",
                        6.0,
                    ),
                )
                critical_usd = max(
                    min_alert_usd,
                    self.config.get_float(
                        "alerts",
                        "liquidation_critical_usd",
                        2_000_000,
                    ),
                )
                if (
                    severity_ratio >= critical_ratio * 1.5
                    or current_usd >= critical_usd * 3
                ):
                    level = "emergency"
                elif severity_ratio >= critical_ratio or current_usd >= critical_usd:
                    level = "critical"
                else:
                    level = "warning"

                breakdown = ", ".join(
                    f"{name.upper()} {self._fmt_usd(value)}"
                    for name, value in list(row["exchange_breakdown"].items())[:5]
                )
                move = (
                    f"，价格 {price_change:+.2f}%" if price_change is not None else ""
                )
                ratio_text = f"{ratio:.1f}x" if ratio is not None else "基准不足"
                message = (
                    f"{symbol} {row['side_label']}爆仓量激增："
                    f"{window_seconds}s {self._fmt_usd(current_usd)}，"
                    f"常态 {ratio_text}；{breakdown}{move}"
                )
                alerts.append(
                    {
                        "alert_type": "liquidation_surge",
                        "level": level,
                        "symbol": symbol,
                        "exchange": "multi",
                        "message": message,
                        "details": {
                            **row,
                            "active_exchanges": active_sources,
                            "active_exchange_count": len(active_sources),
                        },
                    }
                )

        ranking.sort(key=lambda row: row["current_usd"], reverse=True)
        alerts.sort(
            key=lambda alert: (
                {"emergency": 0, "critical": 1, "warning": 2}.get(alert["level"], 9),
                -alert["details"]["current_usd"],
            )
        )
        summary = {
            "short_usd": round(
                sum(row["current_usd"] for row in ranking if row["side"] == "short"), 2
            ),
            "long_usd": round(
                sum(row["current_usd"] for row in ranking if row["side"] == "long"), 2
            ),
            "max_order_usd": round(
                max((row["max_order_usd"] for row in ranking), default=0.0), 2
            ),
            "symbols": len({row["symbol"] for row in ranking}),
        }
        self.last_scan = {
            "timestamp": now_ms,
            "window_seconds": window_seconds,
            "baseline_minutes": baseline_minutes,
            "active_exchanges": active_sources,
            "ranking": ranking[:500],
            "summary": summary,
            "alert_candidates": len(alerts),
        }

        self._flush_buckets()
        if alerts:
            for callback in self.alert_callbacks:
                try:
                    callback(alerts)
                except Exception as e:
                    logger.error(f"Liquidation alert callback error: {e}")
        return self.last_scan

    def _alert_sides(self) -> set:
        value = self.config.get("alerts", "liquidation_alert_sides", ["short", "long"])
        if isinstance(value, str):
            value = [x.strip().lower() for x in value.split(",") if x.strip()]
        if not isinstance(value, (list, tuple, set)):
            value = ["short", "long"]
        return {
            str(x).strip().lower()
            for x in value
            if str(x).strip().lower() in ("short", "long")
        }

    def _weighted_price(self, events: List[Dict[str, Any]]) -> Optional[float]:
        priced = [e for e in events if e.get("price", 0) > 0]
        total_usd = sum(e["usd"] for e in priced)
        if total_usd <= 0:
            return None
        return sum(e["price"] * e["usd"] for e in priced) / total_usd

    def _capture_market_prices(self, symbols: Iterable[str]):
        now_ms = int(time.time() * 1000)
        window_seconds = max(
            10,
            self.config.get_int("alerts", "liquidation_window_seconds", 60),
        )
        horizon_ms = max(180_000, window_seconds * 2_000)
        cutoff_ms = now_ms - horizon_ms
        with self._lock:
            for symbol, samples in list(self._price_samples.items()):
                while samples and samples[0][0] < cutoff_ms:
                    samples.popleft()
                if not samples:
                    del self._price_samples[symbol]

        if not self.data_collector:
            return
        market = getattr(self.data_collector, "market_data", {}) or {}
        for symbol in symbols:
            entries = market.get(f"{symbol}/USDT") or market.get(symbol) or {}
            prices = []
            for entry in entries.values():
                try:
                    price = float(entry.get("futures_price") or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    prices.append(price)
            if not prices:
                continue
            with self._lock:
                samples = self._price_samples[symbol]
                samples.append((now_ms, statistics.median(prices)))
                while samples and samples[0][0] < cutoff_ms:
                    samples.popleft()

    def _market_price_change(self, symbol: str, window_seconds: int) -> Optional[float]:
        with self._lock:
            samples = list(self._price_samples.get(symbol) or ())
        if not samples or len(samples) < 2:
            return None
        now_ms = int(time.time() * 1000)
        target = now_ms - window_seconds * 1000
        current = samples[-1][1]
        historical = min(samples, key=lambda sample: abs(sample[0] - target))
        if historical[1] <= 0 or abs(historical[0] - target) > window_seconds * 500:
            return None
        return (current / historical[1] - 1) * 100

    # ---------- persistence ----------

    def _flush_buckets(self):
        with self._lock:
            ready = list(self._minute_buckets.items())
            self._minute_buckets.clear()
        if not ready:
            return

        rows = [
            (
                key[0],
                key[1],
                key[2],
                key[3],
                round(value["usd"], 8),
                int(value["count"]),
                round(value["max_order_usd"], 8),
            )
            for key, value in ready
        ]
        try:
            self.db.execute_many(
                """
                INSERT INTO liquidation_minutes
                    (bucket, exchange, symbol, side, usd_value, event_count, max_order_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket, exchange, symbol, side) DO UPDATE SET
                    usd_value = liquidation_minutes.usd_value + excluded.usd_value,
                    event_count = liquidation_minutes.event_count + excluded.event_count,
                    max_order_usd = MAX(liquidation_minutes.max_order_usd, excluded.max_order_usd)
                """,
                rows,
            )
        except Exception as e:
            with self._lock:
                for key, value in ready:
                    current = self._minute_buckets.setdefault(
                        key,
                        {
                            "usd": 0.0,
                            "count": 0,
                            "max_order_usd": 0.0,
                        },
                    )
                    current["usd"] += value["usd"]
                    current["count"] += value["count"]
                    current["max_order_usd"] = max(
                        current["max_order_usd"],
                        value["max_order_usd"],
                    )
            logger.warning(f"Liquidation bucket flush failed: {e}")

    def get_recent_aggregates(
        self,
        hours: int = 24,
        limit: int = 200,
        symbol: str = "",
        side: str = "",
    ) -> List[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        minute_ms = 60_000
        since = (
            (now_ms - hours * 3_600_000 + minute_ms - 1) // minute_ms
        ) * minute_ms
        until = now_ms // minute_ms * minute_ms
        sql = """
            SELECT bucket, symbol, side,
                   SUM(usd_value) AS usd_value,
                   SUM(event_count) AS event_count,
                   MAX(max_order_usd) AS max_order_usd
            FROM liquidation_minutes
            WHERE bucket >= ? AND bucket <= ?
        """
        params: List[Any] = [since, until]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol.upper())
        if side in ("long", "short"):
            sql += " AND side = ?"
            params.append(side)
        sql += " GROUP BY bucket, symbol, side ORDER BY bucket DESC, usd_value DESC LIMIT ?"
        params.append(limit)
        return self.db.execute_query(sql, tuple(params))

    def get_window_totals(self, now_ms: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
        """Sum persisted minute buckets across all venues for the last 1/4/24h."""
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        minute_ms = 60_000
        end_bucket = now_ms // minute_ms * minute_ms
        windows = ((1, "1h"), (4, "4h"), (24, "24h"))
        starts = {
            label: ((now_ms - hours * 3_600_000 + minute_ms - 1) // minute_ms)
            * minute_ms
            for hours, label in windows
        }

        result = {
            label: {
                "short_usd": 0.0,
                "long_usd": 0.0,
                "total_usd": 0.0,
                "event_count": 0,
                "max_order_usd": 0.0,
                "from_bucket": starts[label],
                "to_bucket": end_bucket,
                "resolution_seconds": 60,
            }
            for _, label in windows
        }
        if not self.db or starts["24h"] > end_bucket:
            return result

        fields = []
        params: List[Any] = []
        for _, label in windows:
            cutoff = starts[label]
            expressions = (
                (
                    "short_usd",
                    "SUM(CASE WHEN bucket >= ? AND side = 'short' "
                    "THEN usd_value ELSE 0 END)",
                ),
                (
                    "long_usd",
                    "SUM(CASE WHEN bucket >= ? AND side = 'long' "
                    "THEN usd_value ELSE 0 END)",
                ),
                ("total_usd", "SUM(CASE WHEN bucket >= ? THEN usd_value ELSE 0 END)"),
                (
                    "event_count",
                    "SUM(CASE WHEN bucket >= ? THEN event_count ELSE 0 END)",
                ),
                (
                    "max_order_usd",
                    "MAX(CASE WHEN bucket >= ? THEN max_order_usd END)",
                ),
            )
            for field, expression in expressions:
                fields.append(f"COALESCE({expression}, 0) AS {field}_{label}")
                params.append(cutoff)

        rows = self.db.execute_query(
            f"""
            SELECT {', '.join(fields)}
            FROM liquidation_minutes
            WHERE bucket >= ? AND bucket <= ?
            """,
            tuple(params + [starts["24h"], end_bucket]),
        )
        if not rows:
            return result
        row = rows[0]
        for _, label in windows:
            for field in (
                "short_usd",
                "long_usd",
                "total_usd",
                "event_count",
                "max_order_usd",
            ):
                value = row.get(f"{field}_{label}") or 0
                result[label][field] = (
                    int(value) if field == "event_count" else round(float(value), 2)
                )
        return result

    def get_symbol_window_totals(
        self,
        now_ms: Optional[int] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return persisted 1/4/24-hour liquidation totals for each symbol."""
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        minute_ms = 60_000
        end_bucket = now_ms // minute_ms * minute_ms
        windows = ((1, "1h"), (4, "4h"), (24, "24h"))
        starts = {
            label: ((now_ms - hours * 3_600_000 + minute_ms - 1) // minute_ms)
            * minute_ms
            for hours, label in windows
        }
        if not self.db or starts["24h"] > end_bucket:
            return []

        fields = ["symbol"]
        params: List[Any] = []
        for _, label in windows:
            cutoff = starts[label]
            expressions = (
                (
                    "short_usd",
                    "SUM(CASE WHEN bucket >= ? AND side = 'short' "
                    "THEN usd_value ELSE 0 END)",
                ),
                (
                    "long_usd",
                    "SUM(CASE WHEN bucket >= ? AND side = 'long' "
                    "THEN usd_value ELSE 0 END)",
                ),
                ("total_usd", "SUM(CASE WHEN bucket >= ? THEN usd_value ELSE 0 END)"),
                (
                    "event_count",
                    "SUM(CASE WHEN bucket >= ? THEN event_count ELSE 0 END)",
                ),
                (
                    "max_order_usd",
                    "MAX(CASE WHEN bucket >= ? THEN max_order_usd END)",
                ),
            )
            for field, expression in expressions:
                fields.append(f"COALESCE({expression}, 0) AS {field}_{label}")
                params.append(cutoff)

        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 500
        rows = self.db.execute_query(
            f"""
            SELECT {', '.join(fields)}
            FROM liquidation_minutes
            WHERE bucket >= ? AND bucket <= ?
            GROUP BY symbol
            ORDER BY total_usd_24h DESC, symbol ASC
            LIMIT ?
            """,
            tuple(params + [starts["24h"], end_bucket, limit]),
        )

        result = []
        for row in rows:
            item: Dict[str, Any] = {"symbol": row.get("symbol")}
            for _, label in windows:
                item[label] = {}
                for field in (
                    "short_usd",
                    "long_usd",
                    "total_usd",
                    "event_count",
                    "max_order_usd",
                ):
                    value = row.get(f"{field}_{label}") or 0
                    item[label][field] = (
                        int(value) if field == "event_count" else round(float(value), 8)
                    )
            result.append(item)
        return result

    # ---------- helpers ----------

    @staticmethod
    def _base_symbol(value: Any) -> str:
        raw = str(value or "").upper().strip()
        if not raw:
            return ""
        if raw.endswith("-SWAP"):
            raw = raw[:-5]
        raw = raw.replace("/", "").replace("_", "").replace("-", "")
        for quote in ("USDT", "USDC", "USD"):
            if raw.endswith(quote) and len(raw) > len(quote):
                raw = raw[: -len(quote)]
                break
        # Exchanges often quote the same underlying as 1000SHIB, 10000SATS
        # or 1000000MOG while another venue uses the base ticker directly.
        for multiplier in ("1000000", "100000", "10000", "1000"):
            if raw.startswith(multiplier) and len(raw) > len(multiplier):
                return raw[len(multiplier) :]
        return raw

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _fmt_usd(value: float) -> str:
        value = float(value or 0)
        if value >= 1_000_000_000:
            return f"${value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"${value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"${value / 1_000:.1f}K"
        return f"${value:.0f}"
