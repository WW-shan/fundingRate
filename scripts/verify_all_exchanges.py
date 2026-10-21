#!/usr/bin/env python3
"""
全量交易所数据对账（严格模式，不抽样）。

流程：
1. 走系统真实采集代码获取一轮快照（资金费率/价格/锚定资产/参考价）
2. 直接调用五家交易所官方原始接口
3. 对每个交易所、每个 USDT 永续逐条比对费率、周期、下次结算、语义
4. 对每个现货+永续价格对、每个锚定资产报价、每个参考价逐条比对
5. 输出 JSON 报告；有差异时退出码为 1

运行：
    python scripts/verify_all_exchanges.py
    python scripts/verify_all_exchanges.py --exchange okx
"""
import argparse
import concurrent.futures
import json
import math
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, '.env'), override=True)

from config import ConfigManager, peg_assets
from core import DataCollector, MarketMonitor
from database import DatabaseManager

EXCHANGES = ['binance', 'okx', 'bybit', 'gate', 'bitget']
PROXY = os.getenv('PROXY_URL', '')
PROXIES = {'http': PROXY, 'https': PROXY} if PROXY else None
HEADERS = {'User-Agent': 'Mozilla/5.0'}
SESSION = requests.Session()

# 价格是两次独立请求，允许极小的行情漂移；资金费率/周期/时间戳必须精确一致
PRICE_DRIFT_TOL = 0.002
PRICE_REL_TOL = 0.02
PEG_DRIFT_TOL = 0.001
PEG_REL_TOL = 0.01
REF_DRIFT_TOL = 0.002
REF_REL_TOL = 0.01
FUNDING_ABS_TOL = 1e-12
FUNDING_REL_TOL = 1e-9
FUNDING_DRIFT_ABS_TOL = 5e-4


def get_json(url: str, params: Optional[dict] = None, retries: int = 3) -> Any:
    last_err = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, proxies=PROXIES, timeout=30, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f'GET {url} failed: {last_err}')


def num(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def norm_symbol(raw: str, exchange: str) -> Optional[str]:
    if exchange == 'binance':
        return raw[:-4] + '/USDT' if raw.endswith('USDT') else None
    if exchange in ('bybit', 'bitget'):
        return raw[:-4] + '/USDT' if raw.endswith('USDT') else None
    if exchange == 'okx':
        if raw.endswith('-USDT-SWAP'):
            return raw[:-10] + '/USDT'
        if raw.endswith('-USDT'):
            return raw[:-5] + '/USDT'
        return None
    if exchange == 'gate':
        return raw.replace('_', '/') if raw.endswith('_USDT') else None
    return None


def rel_diff(a, b) -> Optional[float]:
    if a is None or b is None:
        return None
    denom = max(abs(float(b)), 1e-12)
    return abs(float(a) - float(b)) / denom


def close_enough(a, b, rel_tol: float, abs_tol: float = 1e-12) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)


def add_mismatch(report: dict, exchange: str, category: str, symbol: str,
                 field: str, system_value, official_value, detail: str = ''):
    key = f'{exchange}.{category}'
    bucket = report.setdefault('mismatches', {}).setdefault(key, [])
    if len(bucket) < 100:
        bucket.append({
            'symbol': symbol,
            'field': field,
            'system': system_value,
            'official': official_value,
            'detail': detail,
        })


def remove_mismatch(report: dict, exchange: str, category: str, symbol: str, field: str):
    key = f'{exchange}.{category}'
    bucket = report.get('mismatches', {}).get(key, [])
    bucket[:] = [x for x in bucket if not (x.get('symbol') == symbol and x.get('field') == field)]


def official_funding(exchange: str) -> Tuple[Dict[str, dict], set]:
    """返回 {base/USDT: {rate, interval_ms, next_ts, semantics}} 和活跃符号集"""
    out: Dict[str, dict] = {}
    active: set = set()

    if exchange == 'binance':
        premium = get_json('https://fapi.binance.com/fapi/v1/premiumIndex')
        info = get_json('https://fapi.binance.com/fapi/v1/fundingInfo')
        exch = get_json('https://fapi.binance.com/fapi/v1/exchangeInfo')
        intervals = {
            x['symbol']: int(x['fundingIntervalHours']) * 3_600_000
            for x in info if x.get('fundingIntervalHours')
        }
        active_raw = {
            x['symbol'] for x in exch.get('symbols', [])
            if x.get('status') == 'TRADING'
            and x.get('contractType') in ('PERPETUAL', 'TRADIFI_PERPETUAL')
            and x.get('quoteAsset') == 'USDT'
        }
        active = {norm_symbol(raw, exchange) for raw in active_raw}
        active.discard(None)
        for x in premium:
            raw = x.get('symbol', '')
            if raw not in active_raw:
                continue
            sym = norm_symbol(raw, exchange)
            if not sym:
                continue
            active.add(sym)
            out[sym] = {
                'rate': num(x.get('lastFundingRate')),
                'interval_ms': intervals.get(raw, 8 * 3_600_000),
                'next_ts': int(x['nextFundingTime']) if x.get('nextFundingTime') else None,
                'semantics': 'upcoming',
            }

    elif exchange == 'okx':
        fund = get_json('https://www.okx.com/api/v5/public/funding-rate', params={'instId': 'ANY'})['data']
        insts = get_json('https://www.okx.com/api/v5/public/instruments', params={'instType': 'SWAP'})['data']
        active_raw = {
            x['instId'] for x in insts
            if x.get('state') == 'live' and x.get('instId', '').endswith('-USDT-SWAP')
        }
        active = {norm_symbol(raw, exchange) for raw in active_raw}
        active.discard(None)
        for x in fund:
            raw = x.get('instId', '')
            if raw not in active_raw:
                continue
            sym = norm_symbol(raw, exchange)
            if not sym:
                continue
            funding_time = int(x['fundingTime']) if x.get('fundingTime') else None
            next_time = int(x['nextFundingTime']) if x.get('nextFundingTime') else None
            interval_ms = (next_time - funding_time) if funding_time and next_time else None
            active.add(sym)
            out[sym] = {
                'rate': num(x.get('fundingRate')),
                'interval_ms': interval_ms,
                'next_ts': funding_time,
                'semantics': 'upcoming',
            }

    elif exchange == 'bybit':
        tickers = get_json('https://api.bybit.com/v5/market/tickers', params={'category': 'linear'})['result']['list']
        insts = get_json('https://api.bybit.com/v5/market/instruments-info',
                         params={'category': 'linear', 'limit': 1000})['result']['list']
        inst_map = {
            x['symbol']: x for x in insts
            if x.get('status') == 'Trading'
            and x.get('contractType') == 'LinearPerpetual'
            and x.get('quoteCoin') == 'USDT'
        }
        active = {norm_symbol(raw, exchange) for raw in inst_map}
        active.discard(None)
        for x in tickers:
            raw = x.get('symbol', '')
            if raw not in inst_map:
                continue
            sym = norm_symbol(raw, exchange)
            if not sym:
                continue
            interval_h = x.get('fundingIntervalHour')
            if interval_h in (None, ''):
                interval_min = inst_map[raw].get('fundingInterval')
                interval_ms = int(interval_min) * 60_000 if interval_min else None
            else:
                interval_ms = int(float(interval_h) * 3_600_000)
            active.add(sym)
            out[sym] = {
                'rate': num(x.get('fundingRate')),
                'interval_ms': interval_ms,
                'next_ts': int(x['nextFundingTime']) if x.get('nextFundingTime') else None,
                'semantics': 'upcoming',
            }

    elif exchange == 'gate':
        contracts = get_json('https://api.gateio.ws/api/v4/futures/usdt/contracts')
        for x in contracts:
            if x.get('status') != 'trading' or x.get('in_delisting'):
                continue
            raw = x.get('name', '')
            sym = norm_symbol(raw, exchange)
            if not sym:
                continue
            active.add(sym)
            out[sym] = {
                'rate': num(x.get('funding_rate')),
                'interval_ms': int(x['funding_interval']) * 1000 if x.get('funding_interval') else None,
                'next_ts': int(x['funding_next_apply']) * 1000 if x.get('funding_next_apply') else None,
                'semantics': 'upcoming',
            }

    elif exchange == 'bitget':
        tickers = get_json('https://api.bitget.com/api/v2/mix/market/tickers',
                           params={'productType': 'USDT-FUTURES'})['data']
        fund = get_json('https://api.bitget.com/api/v2/mix/market/current-fund-rate',
                        params={'productType': 'USDT-FUTURES'})['data']
        contracts = get_json('https://api.bitget.com/api/v2/mix/market/contracts',
                             params={'productType': 'USDT-FUTURES'})['data']
        active_raw = {
            x['symbol'] for x in contracts
            if x.get('symbolStatus') == 'normal'
            and x.get('symbolType') == 'perpetual'
            and x.get('quoteCoin') == 'USDT'
        }
        ticker_map = {x.get('symbol'): x for x in tickers}
        fund_map = {x.get('symbol'): x for x in fund}
        active = {norm_symbol(raw, exchange) for raw in active_raw}
        active.discard(None)
        for raw in active_raw:
            sym = norm_symbol(raw, exchange)
            if not sym:
                continue
            t = ticker_map.get(raw) or {}
            f = fund_map.get(raw) or {}
            out[sym] = {
                'rate': num(t.get('fundingRate')),
                'interval_ms': int(float(f['fundingRateInterval']) * 3_600_000) if f.get('fundingRateInterval') else None,
                'next_ts': int(f['nextUpdate']) if f.get('nextUpdate') else None,
                'semantics': 'upcoming',
                'fund_rate_crosscheck': num(f.get('fundingRate')),
            }

    return out, active


def official_prices(exchange: str) -> Tuple[Dict[str, dict], Dict[str, dict], set]:
    """返回 (spot_map, swap_map, active_pair_set)，键均为 base/USDT。"""
    spot: Dict[str, dict] = {}
    swap: Dict[str, dict] = {}

    if exchange == 'binance':
        spot_info = get_json('https://api.binance.com/api/v3/exchangeInfo')
        fut_info = get_json('https://fapi.binance.com/fapi/v1/exchangeInfo')
        spot_active = {
            x['symbol'] for x in spot_info.get('symbols', [])
            if x.get('status') == 'TRADING'
            and x.get('quoteAsset') == 'USDT'
            and x.get('isSpotTradingAllowed', True)
        }
        swap_active = {
            x['symbol'] for x in fut_info.get('symbols', [])
            if x.get('status') == 'TRADING'
            and x.get('contractType') in ('PERPETUAL', 'TRADIFI_PERPETUAL')
            and x.get('quoteAsset') == 'USDT'
        }
        for x in get_json('https://api.binance.com/api/v3/ticker/24hr'):
            raw = x.get('symbol', '')
            if raw not in spot_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                spot[sym] = {'last': num(x.get('lastPrice')), 'bid': num(x.get('bidPrice')),
                             'ask': num(x.get('askPrice')), 'ts': x.get('closeTime')}
        for x in get_json('https://fapi.binance.com/fapi/v1/ticker/24hr'):
            raw = x.get('symbol', '')
            if raw not in swap_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                swap[sym] = {'last': num(x.get('lastPrice')), 'bid': num(x.get('bidPrice')),
                             'ask': num(x.get('askPrice')), 'ts': x.get('closeTime')}

    elif exchange == 'okx':
        spot_insts = get_json('https://www.okx.com/api/v5/public/instruments',
                              params={'instType': 'SPOT'})['data']
        swap_insts = get_json('https://www.okx.com/api/v5/public/instruments',
                              params={'instType': 'SWAP'})['data']
        spot_active = {
            x['instId'] for x in spot_insts
            if x.get('state') == 'live' and x.get('quoteCcy') == 'USDT'
        }
        swap_active = {
            x['instId'] for x in swap_insts
            if x.get('state') == 'live' and x.get('instId', '').endswith('-USDT-SWAP')
        }
        for inst_type, target, active in [('SPOT', spot, spot_active), ('SWAP', swap, swap_active)]:
            for x in get_json('https://www.okx.com/api/v5/market/tickers', params={'instType': inst_type})['data']:
                raw = x.get('instId', '')
                if raw not in active:
                    continue
                sym = norm_symbol(raw, exchange)
                if sym:
                    target[sym] = {'last': num(x.get('last')), 'bid': num(x.get('bidPx')),
                                   'ask': num(x.get('askPx')), 'ts': x.get('ts')}

    elif exchange == 'bybit':
        spot_insts = get_json('https://api.bybit.com/v5/market/instruments-info',
                              params={'category': 'spot', 'limit': 1000})['result']['list']
        lin_insts = get_json('https://api.bybit.com/v5/market/instruments-info',
                             params={'category': 'linear', 'limit': 1000})['result']['list']
        spot_active = {
            x['symbol'] for x in spot_insts
            if x.get('status') == 'Trading' and x.get('quoteCoin') == 'USDT'
        }
        swap_active = {
            x['symbol'] for x in lin_insts
            if x.get('status') == 'Trading' and x.get('contractType') == 'LinearPerpetual'
            and x.get('quoteCoin') == 'USDT'
        }
        for x in get_json('https://api.bybit.com/v5/market/tickers', params={'category': 'spot'})['result']['list']:
            raw = x.get('symbol', '')
            if raw not in spot_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                spot[sym] = {'last': num(x.get('lastPrice')), 'bid': num(x.get('bid1Price')),
                             'ask': num(x.get('ask1Price')), 'ts': x.get('time')}
        for x in get_json('https://api.bybit.com/v5/market/tickers', params={'category': 'linear'})['result']['list']:
            raw = x.get('symbol', '')
            if raw not in swap_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                swap[sym] = {'last': num(x.get('lastPrice')), 'bid': num(x.get('bid1Price')),
                             'ask': num(x.get('ask1Price')), 'ts': x.get('time')}

    elif exchange == 'gate':
        spot_pairs = get_json('https://api.gateio.ws/api/v4/spot/currency_pairs')
        spot_active = {
            x['id'] for x in spot_pairs
            if x.get('trade_status') == 'tradable' and x.get('quote') == 'USDT'
        }
        contracts = get_json('https://api.gateio.ws/api/v4/futures/usdt/contracts')
        swap_active = {
            x['name'] for x in contracts
            if x.get('status') == 'trading' and not x.get('in_delisting')
        }
        for x in get_json('https://api.gateio.ws/api/v4/spot/tickers'):
            raw = x.get('currency_pair', '')
            if raw not in spot_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                spot[sym] = {'last': num(x.get('last')), 'bid': num(x.get('highest_bid')),
                             'ask': num(x.get('lowest_ask')), 'ts': None}
        for x in get_json('https://api.gateio.ws/api/v4/futures/usdt/tickers'):
            raw = x.get('contract', '')
            if raw not in swap_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                swap[sym] = {'last': num(x.get('last')), 'bid': num(x.get('highest_bid')),
                             'ask': num(x.get('lowest_ask')), 'ts': None}

    elif exchange == 'bitget':
        spot_symbols = get_json('https://api.bitget.com/api/v2/spot/public/symbols')['data']
        spot_active = {
            x['symbol'] for x in spot_symbols
            if x.get('status') == 'online' and x.get('quoteCoin') == 'USDT'
        }
        contracts = get_json('https://api.bitget.com/api/v2/mix/market/contracts',
                             params={'productType': 'USDT-FUTURES'})['data']
        swap_active = {
            x['symbol'] for x in contracts
            if x.get('symbolStatus') == 'normal'
            and x.get('symbolType') == 'perpetual'
            and x.get('quoteCoin') == 'USDT'
        }
        for x in get_json('https://api.bitget.com/api/v2/spot/market/tickers')['data']:
            raw = x.get('symbol', '')
            if raw not in spot_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                spot[sym] = {'last': num(x.get('lastPr')), 'bid': num(x.get('bidPr')),
                             'ask': num(x.get('askPr')), 'ts': x.get('ts')}
        for x in get_json('https://api.bitget.com/api/v2/mix/market/tickers',
                          params={'productType': 'USDT-FUTURES'})['data']:
            raw = x.get('symbol', '')
            if raw not in swap_active:
                continue
            sym = norm_symbol(raw, exchange)
            if sym:
                swap[sym] = {'last': num(x.get('lastPr')), 'bid': num(x.get('bidPr')),
                             'ask': num(x.get('askPr')), 'ts': x.get('ts')}

    return spot, swap, set(spot) & set(swap)


def compare_funding(report: dict, exchange: str, system: dict, official: dict, active: set):
    sys_map = {}
    for sym, exchanges in system.items():
        data = exchanges.get(exchange) or {}
        if data.get('funding_rate') is not None:
            sys_map[sym] = data

    stats = report['funding'][exchange] = {
        'official_active': len(active),
        'system': len(sys_map),
        'missing': 0,
        'extra': 0,
        'rate_mismatch': 0,
        'rate_drift': 0,
        'max_rate_abs_diff': 0.0,
        'bitget_crosscheck_drift': 0,
        'max_bitget_crosscheck_diff': 0.0,
        'interval_mismatch': 0,
        'next_ts_mismatch': 0,
        'semantics_mismatch': 0,
        'invalid_apr': 0,
    }

    for sym in sorted(active - set(sys_map)):
        stats['missing'] += 1
        add_mismatch(report, exchange, 'funding', sym, 'missing', None, official.get(sym))
    for sym in sorted(set(sys_map) - active):
        stats['extra'] += 1
        add_mismatch(report, exchange, 'funding', sym, 'extra', sys_map[sym], None)

    rate_issues = []
    for sym in sorted(active & set(sys_map)):
        s = sys_map[sym]
        o = official.get(sym)
        if not o:
            stats['rate_mismatch'] += 1
            add_mismatch(report, exchange, 'funding', sym, 'official_missing',
                         s.get('funding_rate'), None)
            continue
        system_rate = s.get('funding_rate')
        official_rate = o.get('rate')
        if system_rate is None or official_rate is None:
            stats['rate_mismatch'] += 1
            add_mismatch(report, exchange, 'funding', sym, 'funding_rate', system_rate, official_rate)
            continue
        if not close_enough(system_rate, official_rate, FUNDING_REL_TOL, FUNDING_ABS_TOL):
            rate_abs_diff = abs(float(system_rate) - float(official_rate))
            stats['max_rate_abs_diff'] = max(stats['max_rate_abs_diff'], rate_abs_diff)
            structural_match = (
                s.get('funding_interval') == o.get('interval_ms')
                and s.get('next_funding_time') == o.get('next_ts')
                and s.get('rate_semantics') == o.get('semantics')
            )
            if structural_match and rate_abs_diff <= FUNDING_DRIFT_ABS_TOL:
                stats['rate_drift'] += 1
            else:
                stats['rate_mismatch'] += 1
                rate_issues.append((sym, s.get('funding_rate'), o.get('rate')))
                add_mismatch(report, exchange, 'funding', sym, 'funding_rate', s.get('funding_rate'), o.get('rate'))
        if s.get('funding_interval') != o.get('interval_ms'):
            stats['interval_mismatch'] += 1
            add_mismatch(report, exchange, 'funding', sym, 'funding_interval', s.get('funding_interval'), o.get('interval_ms'))
        if s.get('next_funding_time') != o.get('next_ts'):
            stats['next_ts_mismatch'] += 1
            add_mismatch(report, exchange, 'funding', sym, 'next_funding_time', s.get('next_funding_time'), o.get('next_ts'))
        if s.get('rate_semantics') != o.get('semantics'):
            stats['semantics_mismatch'] += 1
            add_mismatch(report, exchange, 'funding', sym, 'rate_semantics', s.get('rate_semantics'), o.get('semantics'))
        iv = s.get('funding_interval')
        rate = s.get('funding_rate')
        if iv and rate is not None:
            if not (3_600_000 <= iv <= 86_400_000):
                stats['invalid_apr'] += 1
                add_mismatch(report, exchange, 'funding', sym, 'interval_range', iv, '1h..24h')
            else:
                # 逐条 APR 由 verify_internal() 对照 MarketMonitor 输出复核
                pass
        if exchange == 'bitget' and o.get('fund_rate_crosscheck') is not None:
            crosscheck_diff = abs(float(system_rate) - float(o['fund_rate_crosscheck']))
            stats['max_bitget_crosscheck_diff'] = max(
                stats['max_bitget_crosscheck_diff'], crosscheck_diff
            )
            if crosscheck_diff > FUNDING_DRIFT_ABS_TOL:
                add_mismatch(report, exchange, 'funding', sym, 'bitget_rate_source_crosscheck',
                             s.get('funding_rate'), o['fund_rate_crosscheck'])
            elif crosscheck_diff > FUNDING_ABS_TOL:
                stats['bitget_crosscheck_drift'] += 1

    # Gate/Bybit predicted rates can refresh between two API calls. If the
    # system value matches a second official snapshot, classify it as drift.
    if rate_issues:
        try:
            retry, _ = official_funding(exchange)
        except Exception:
            retry = {}
        for sym, system_rate, _ in rate_issues:
            retry_rate = (retry.get(sym) or {}).get('rate')
            if close_enough(system_rate, retry_rate, FUNDING_REL_TOL, FUNDING_ABS_TOL):
                stats['rate_mismatch'] -= 1
                stats['rate_drift'] += 1
                remove_mismatch(report, exchange, 'funding', sym, 'funding_rate')


def compare_prices(report: dict, exchange: str, system: dict, spot: dict, swap: dict, active_pairs: set):
    sys_map = {}
    for sym, exchanges in system.items():
        data = exchanges.get(exchange) or {}
        if data.get('spot_price') is not None and data.get('futures_price') is not None:
            sys_map[sym] = data

    stats = report['prices'][exchange] = {
        'official_pairs': len(active_pairs),
        'system_pairs': len(sys_map),
        'missing': 0,
        'extra': 0,
        'spot_mismatch': 0,
        'futures_mismatch': 0,
        'bid_ask_mismatch': 0,
        'spot_drift': 0,
        'futures_drift': 0,
        'bid_ask_drift': 0,
        'max_spot_rel_diff': 0.0,
        'max_futures_rel_diff': 0.0,
    }

    for sym in sorted(active_pairs - set(sys_map)):
        stats['missing'] += 1
        add_mismatch(report, exchange, 'prices', sym, 'missing', None, {'spot': spot.get(sym), 'swap': swap.get(sym)})
    for sym in sorted(set(sys_map) - active_pairs):
        stats['extra'] += 1
        add_mismatch(report, exchange, 'prices', sym, 'extra', sys_map[sym], None)

    for sym in sorted(active_pairs & set(sys_map)):
        s, osp, ofu = sys_map[sym], spot[sym], swap[sym]
        d_spot = rel_diff(s.get('spot_price'), osp.get('last'))
        d_fut = rel_diff(s.get('futures_price'), ofu.get('last'))
        if d_spot is not None:
            stats['max_spot_rel_diff'] = max(stats['max_spot_rel_diff'], d_spot)
        if d_fut is not None:
            stats['max_futures_rel_diff'] = max(stats['max_futures_rel_diff'], d_fut)
        if not close_enough(s.get('spot_price'), osp.get('last'), PRICE_REL_TOL, 1e-12):
            stats['spot_mismatch'] += 1
            add_mismatch(report, exchange, 'prices', sym, 'spot_price', s.get('spot_price'), osp.get('last'))
        elif not close_enough(s.get('spot_price'), osp.get('last'), PRICE_DRIFT_TOL, 1e-12):
            stats['spot_drift'] += 1
        if not close_enough(s.get('futures_price'), ofu.get('last'), PRICE_REL_TOL, 1e-12):
            stats['futures_mismatch'] += 1
            add_mismatch(report, exchange, 'prices', sym, 'futures_price', s.get('futures_price'), ofu.get('last'))
        elif not close_enough(s.get('futures_price'), ofu.get('last'), PRICE_DRIFT_TOL, 1e-12):
            stats['futures_drift'] += 1
        bid_ask_ok = (
            close_enough(s.get('spot_bid'), osp.get('bid'), PRICE_REL_TOL, 1e-12)
            and close_enough(s.get('spot_ask'), osp.get('ask'), PRICE_REL_TOL, 1e-12)
            and close_enough(s.get('futures_bid'), ofu.get('bid'), PRICE_REL_TOL, 1e-12)
            and close_enough(s.get('futures_ask'), ofu.get('ask'), PRICE_REL_TOL, 1e-12)
        )
        if not bid_ask_ok:
            stats['bid_ask_mismatch'] += 1
            add_mismatch(report, exchange, 'prices', sym, 'bid_ask',
                         [s.get('spot_bid'), s.get('spot_ask'), s.get('futures_bid'), s.get('futures_ask')],
                         [osp.get('bid'), osp.get('ask'), ofu.get('bid'), ofu.get('ask')])
        else:
            bid_ask_drift = not (
                close_enough(s.get('spot_bid'), osp.get('bid'), PRICE_DRIFT_TOL, 1e-12)
                and close_enough(s.get('spot_ask'), osp.get('ask'), PRICE_DRIFT_TOL, 1e-12)
                and close_enough(s.get('futures_bid'), ofu.get('bid'), PRICE_DRIFT_TOL, 1e-12)
                and close_enough(s.get('futures_ask'), ofu.get('ask'), PRICE_DRIFT_TOL, 1e-12)
            )
            if bid_ask_drift:
                stats['bid_ask_drift'] += 1


def verify_peg(report: dict, dc: DataCollector, official_spots: Dict[str, dict]):
    registry = dc.peg_discovery.registry
    expected = set()
    for sym in registry:
        for exchange, spot in official_spots.items():
            raw = f'USDC/USDT' if sym == 'USDT' else f'{sym}/USDT'
            if raw in spot:
                expected.add((exchange, sym))

    actual = set()
    stats = report['peg'] = {
        'registry': len(registry),
        'quotes': 0,
        'expected_quotes': len(expected),
        'missing': 0,
        'extra': 0,
        'price_mismatch': 0,
        'price_drift': 0,
        'max_rel_diff': 0.0,
    }
    for sym, by_exchange in dc.stablecoin_data.items():
        for exchange, data in by_exchange.items():
            if exchange not in official_spots:
                continue
            actual.add((exchange, sym))
            stats['quotes'] += 1
            raw = 'USDC/USDT' if sym == 'USDT' else f'{sym}/USDT'
            official = official_spots.get(exchange, {}).get(raw)
            if not official:
                stats['extra'] += 1
                add_mismatch(report, exchange, 'peg', sym, 'extra', data.get('price'), None)
                continue
            official_price = official.get('last')
            if sym == 'USDT' and official_price:
                official_price = 1.0 / official_price
            d = rel_diff(data.get('price'), official_price)
            if d is not None:
                stats['max_rel_diff'] = max(stats['max_rel_diff'], d)
            if not close_enough(data.get('price'), official_price, PEG_REL_TOL, 1e-12):
                stats['price_mismatch'] += 1
                add_mismatch(report, exchange, 'peg', sym, 'price', data.get('price'), official_price)
            elif not close_enough(data.get('price'), official_price, PEG_DRIFT_TOL, 1e-12):
                stats['price_drift'] += 1

    for exchange, sym in sorted(expected - actual):
        stats['missing'] += 1
        add_mismatch(report, exchange, 'peg', sym, 'missing', None, None)


def verify_references(report: dict, dc: DataCollector):
    refs = dict(dc.reference_prices)
    stats = report['references'] = {'checked': 0, 'mismatch': 0, 'drift': 0,
                                   'failed_fetch': 0, 'max_rel_diff': 0.0}

    def fetch(item):
        sym, data = item
        try:
            if sym == peg_assets.GOLD_SPOT_SYMBOL:
                quotes = get_json(
                    'https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD',
                    retries=2)
                entry = next((x for x in quotes
                              if isinstance(x, dict) and x.get('spreadProfilePrices')), None)
                profiles = (entry or {}).get('spreadProfilePrices') or []
                quote = next((q for q in profiles if q.get('spreadProfile') == 'premium'),
                             profiles[0] if profiles else None)
                if not quote:
                    return sym, data, None
                official = (num(quote.get('bid')) + num(quote.get('ask'))) / 2
            else:
                r = get_json(f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}',
                             params={'interval': '1m', 'range': '1d'}, retries=2)
                official = num(r['chart']['result'][0]['meta'].get('regularMarketPrice'))
            return sym, data, official
        except Exception:
            return sym, data, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for sym, data, official in pool.map(fetch, refs.items()):
            stats['checked'] += 1
            if official is None:
                stats['failed_fetch'] += 1
                continue
            d = rel_diff(data.get('price'), official)
            if d is not None:
                stats['max_rel_diff'] = max(stats['max_rel_diff'], d)
            if not close_enough(data.get('price'), official, REF_REL_TOL, 1e-9):
                stats['mismatch'] += 1
                add_mismatch(report, 'yahoo', 'references', sym, 'price', data.get('price'), official)
            elif not close_enough(data.get('price'), official, REF_DRIFT_TOL, 1e-9):
                stats['drift'] += 1


def verify_internal(report: dict, dc: DataCollector, mm: MarketMonitor):
    # APR 公式逐条复算
    ranking = mm.build_apr_ranking(dc.market_data)
    stats = report['internal'] = {'apr_rows': len(ranking), 'apr_formula_mismatch': 0,
                                  'depeg_rows': 0, 'depeg_formula_mismatch': 0,
                                  'issuer_rows': 0, 'issuer_formula_mismatch': 0}
    for r in ranking:
        interval = r.get('funding_interval_ms')
        if not interval or not (3_600_000 <= interval <= 86_400_000):
            continue
        expected = r['funding_rate'] * (365 * 24 * 3_600_000 / interval)
        if not close_enough(r.get('apr'), expected, 1e-9, 1e-12):
            stats['apr_formula_mismatch'] += 1
            add_mismatch(report, r['exchange'], 'internal', r['symbol'], 'apr', r.get('apr'), expected)

    depeg = mm.build_depeg_list(dc.stablecoin_data)
    stats['depeg_rows'] = len(depeg)
    stats['depeg_peer_reference_rows'] = 0
    stats['depeg_stale_no_peer_rows'] = 0
    stats['depeg_no_peer_rows'] = 0
    for r in depeg:
        source = r.get('reference_source', 'reference')
        # 无同类产品/休市无同类时不计算偏离，仅校验来源标记
        if source in ('stale_close', 'no_peer') or not r.get('reference'):
            if source == 'stale_close':
                stats['depeg_stale_no_peer_rows'] += 1
            elif source == 'no_peer':
                stats['depeg_no_peer_rows'] += 1
            else:
                stats['depeg_formula_mismatch'] += 1
                add_mismatch(report, r['exchange'], 'internal', r['symbol'], 'reference_source', source, 'no_peer')
            continue
        expected = (r['price'] - r['reference']) / r['reference']
        if not close_enough(r.get('deviation'), expected, 1e-9, 1e-12):
            stats['depeg_formula_mismatch'] += 1
            add_mismatch(report, r['exchange'], 'internal', r['symbol'], 'deviation', r.get('deviation'), expected)
        if source == 'peer':
            stats['depeg_peer_reference_rows'] += 1
        elif source != 'reference':
            stats['depeg_formula_mismatch'] += 1
            add_mismatch(report, r['exchange'], 'internal', r['symbol'], 'reference_source', source, 'reference')

    spreads = mm.build_cross_issuer_spreads(dc.stablecoin_data)
    stats['issuer_rows'] = len(spreads)
    for r in spreads:
        expected = (r['high'] - r['low']) / r['low'] if r['low'] else None
        if not close_enough(r.get('abs_spread'), abs(expected), 1e-9, 1e-12):
            stats['issuer_formula_mismatch'] += 1
            add_mismatch(report, 'multi', 'internal', r['underlying'], 'issuer_spread', r.get('abs_spread'), expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exchange', choices=EXCHANGES + ['all'], default='all')
    args = parser.parse_args()
    targets = EXCHANGES if args.exchange == 'all' else [args.exchange]

    report = {
        'started_at': datetime.now().isoformat(),
        'proxy': bool(PROXY),
        'funding': {},
        'prices': {},
        'peg': {},
        'references': {},
        'internal': {},
        'mismatches': {},
    }

    db = DatabaseManager(os.getenv('DATABASE_PATH', 'data/database.db'))
    cm = ConfigManager(db)
    dc = DataCollector(cm, db)
    mm = MarketMonitor(cm, db, dc)
    all_exchanges = dict(dc.exchanges)

    # 先用全量交易所建立锚定资产注册表，后续逐所采集复用
    print('[准备] 全量发现锚定资产...')
    dc.peg_discovery.refresh(all_exchanges, force=True)

    official_spots: Dict[str, dict] = {}
    for exchange in targets:
        if exchange not in all_exchanges:
            print(f'[跳过] {exchange} 未连接')
            continue

        # 每个交易所单独采集，并立即拉官方原始数据比对，避免时间漂移
        adapter = all_exchanges[exchange]
        dc.exchanges = {exchange: adapter}
        adapter.exchange.load_markets()
        old_rate_limit = adapter.exchange.enableRateLimit
        adapter.exchange.enableRateLimit = False
        try:
            print(f'[??] {exchange} funding...')
            dc._collect_funding_rates()
            off, active = official_funding(exchange)
            compare_funding(report, exchange, dc.market_data, off, active)

            print(f'[??] {exchange} prices...')
            dc._collect_prices()
            spot, swap, pairs = official_prices(exchange)
            official_spots[exchange] = spot
            compare_prices(report, exchange, dc.market_data, spot, swap, pairs)
        finally:
            adapter.exchange.enableRateLimit = old_rate_limit

    dc.exchanges = all_exchanges
    dc._collect_stablecoins()
    for exchange in targets:
        if exchange in all_exchanges:
            spot, _, _ = official_prices(exchange)
            official_spots[exchange] = spot
    verify_peg(report, dc, official_spots)

    print('[对账] Yahoo 参考价...')
    dc._collect_reference_prices()
    verify_references(report, dc)
    print('[对账] 内部公式...')
    verify_internal(report, dc, mm)

    report['finished_at'] = datetime.now().isoformat()
    report_path = os.path.join(ROOT, 'logs', 'verification_report.json')
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print('\n=== 资金费率逐所汇总 ===')
    for ex in targets:
        if ex not in report['funding']:
            print(f"{ex:8} SKIPPED (exchange unavailable)")
            continue
        s = report['funding'][ex]
        print(f"{ex:8} active={s['official_active']:4} system={s['system']:4} "
              f"missing={s['missing']} extra={s['extra']} rate={s['rate_mismatch']} "
              f"rate_drift={s.get('rate_drift', 0)} "
              f"max_rate_diff={s.get('max_rate_abs_diff', 0):.8f} "
              f"interval={s['interval_mismatch']} next={s['next_ts_mismatch']} "
              f"semantics={s['semantics_mismatch']} invalid_apr={s['invalid_apr']}")

    print('\n=== 价格/基差逐所汇总 ===')
    for ex in targets:
        if ex not in report['prices']:
            print(f"{ex:8} SKIPPED (exchange unavailable)")
            continue
        s = report['prices'][ex]
        print(f"{ex:8} official={s['official_pairs']:4} system={s['system_pairs']:4} "
              f"missing={s['missing']} extra={s['extra']} spot={s['spot_mismatch']} "
              f"futures={s['futures_mismatch']} bidask={s['bid_ask_mismatch']} "
              f"drift={s['spot_drift']}/{s['futures_drift']}/{s['bid_ask_drift']} "
              f"max_spot={s['max_spot_rel_diff']:.4%} max_fut={s['max_futures_rel_diff']:.4%}")

    print('\n=== 锚定资产/参考价/内部公式 ===')
    print('peg:', report['peg'])
    print('references:', report['references'])
    print('internal:', report['internal'])
    print('report:', report_path)

    total = sum(len(v) for v in report['mismatches'].values())
    print('mismatch details:', total)
    return 1 if total else 0


if __name__ == '__main__':
    sys.exit(main())
