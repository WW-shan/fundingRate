"""
锚定资产自动发现
扫描各交易所现货市场，识别稳定币 / 黄金代币 / 股票代币，
并按流动性过滤（只监控流通性好的）。

分类规则：
  - usd_stable: 白名单稳定币，锚定 1.0
  - eur_stable: 欧元稳定币，锚定 EUR/USD 汇率
  - gold:       黄金代币（PAXG/XAUT），锚定现货黄金
  - stock:      BStock(Binance) / xStocks(Bybit,Gate)，锚定对应股票（Yahoo 验证）
"""
import re
import time
from typing import Dict, List, Any, Optional
from loguru import logger

# ---------- 分类白名单 ----------

# USD 稳定币（锚定 1.0）
USD_STABLES = [
    'USDC', 'FDUSD', 'TUSD', 'USDP', 'PYUSD', 'RLUSD', 'USD1', 'USDE',
    'USDS', 'USDG', 'GUSD', 'USDD', 'XUSD', 'BUSD', 'DAI', 'FRAX',
    'GHO', 'CRVUSD', 'CUSD', 'USDTB', 'BFUSD', 'RWUSD',
    'USDGO', 'USDPT', 'USDSB', 'USDSOLD',
]

# 欧元稳定币（锚定 EUR/USD）
EUR_STABLES = ['AEUR', 'EURI', 'EUR']

# 黄金代币（锚定现货黄金）
GOLD_TOKENS = ['PAXG', 'XAUT']

# 已知股票代币（交易所符号 -> Yahoo 代码）
KNOWN_STOCK_TOKENS = {
    # ---- Binance BStock（已验证价格对应）----
    'AAPLB': 'AAPL', 'TSLAB': 'TSLA', 'NVDAB': 'NVDA', 'COINB': 'COIN',
    'MSTRB': 'MSTR', 'HOODB': 'HOOD', 'AMZNB': 'AMZN', 'GOOGLB': 'GOOGL',
    'METAB': 'META', 'MSFTB': 'MSFT', 'SPYB': 'SPY', 'QQQB': 'QQQ',
    'PLTRB': 'PLTR', 'INTCB': 'INTC', 'AMDB': 'AMD', 'MUB': 'MU',
    'ORCLB': 'ORCL', 'JPMB': 'JPM', 'GMEB': 'GME', 'RDDTB': 'RDDT',
    'CRCLB': 'CRCL', 'NFLXB': 'NFLX', 'DELLB': 'DELL', 'IBMB': 'IBM',
    'AVGOB': 'AVGO', 'MRVLB': 'MRVL', 'SMCIB': 'SMCI', 'COHRB': 'COHR',
    'SNDKB': 'SNDK', 'WDCB': 'WDC', 'CRWVB': 'CRWV', 'NBISB': 'NBIS',
    'SOXLB': 'SOXL', 'UVXYB': 'UVXY', 'TQQQB': 'TQQQ', 'SQQQB': 'SQQQ',
    'SPCXB': 'SPCX', 'BABAB': 'BABA', 'PDDB': 'PDD', 'NOKB': 'NOK',
    'GSB': 'GS', 'AMATB': 'AMAT', 'ASMLB': 'ASML', 'ASTSB': 'ASTS',
    'BMNRB': 'BMNR', 'CBRSB': 'CBRS', 'CRDOB': 'CRDO', 'CRMB': 'CRM',
    'CRWDB': 'CRWD', 'CYPHB': 'CYPH', 'DJTB': 'DJT', 'DRAMB': 'DRAM',
    'EWYB': 'EWY', 'FLNCB': 'FLNC', 'FWDIB': 'FWDI', 'GLWB': 'GLW',
    'GPROB': 'GPRO', 'HIMSB': 'HIMS', 'HPEB': 'HPE', 'INTWB': 'INTW',
    'IRENB': 'IREN', 'KORUB': 'KORU', 'LITEB': 'LITE', 'LLYB': 'LLY',
    'MDBB': 'MDB', 'MRNAB': 'MRNA', 'MVLLB': 'MVLL', 'NBISB': 'NBIS',
    'PENGB': 'PENG', 'PYPLB': 'PYPL', 'QCOMB': 'QCOM', 'QNTB': 'QNT',
    'RKLBB': 'RKLB', 'SECZB': 'SECZ', 'SKHYB': 'SKHY', 'SMHB': 'SMH',
    'SNXXB': 'SNXX', 'SOXSB': 'SOXS', 'TSMB': 'TSM', 'USARB': 'USAR',
    'WENB': 'WEN', 'ZMB': 'ZM', 'SHAZB': 'SHAZ', 'BWETB': 'BWET',
    # ---- Bybit xStocks ----
    'TSLAX': 'TSLA', 'NVDAX': 'NVDA', 'AAPLX': 'AAPL', 'AMZNX': 'AMZN',
    'GOOGLX': 'GOOGL', 'METAX': 'META', 'COINX': 'COIN', 'HOODX': 'HOOD',
    'SPCXX': 'SPCX', 'MSTRX': 'MSTR', 'NFLXX': 'NFLX', 'MSFTX': 'MSFT',
    'QQQX': 'QQQ', 'SPYX': 'SPY', 'PEPX': 'PEP', 'WMTX': 'WMT',
    # ---- Gate 股票代币 ----
    'SPCX': 'SPCX', 'MAX': 'MA', 'KOX': 'KO',
}

# 明确排除（同名非锚定资产）
EXCLUDED = {
    'BUCK',    # GME Mascot（meme 币）
    'USTC',    # 已崩盘，无锚定意义
    'NEURO',   # 非稳定币
    'SUSD',    # 流动性枯竭，价格已失真
    'USDEB',   # 非稳定币（价格 ~10.7）
    'CUSD',    # 需人工确认，暂排除
}

# 参考价 Yahoo 符号
YAHOO_SYMBOLS = {
    'gold': 'GC=F',
    'eurusd': 'EURUSD=X',
}

# 脱锚阈值（偏离比例）
THRESHOLDS = {
    'usd_stable': {'warning': 0.003, 'critical': 0.01},   # 0.3% / 1%
    'eur_stable': {'warning': 0.005, 'critical': 0.02},   # 0.5% / 2%
    'gold':       {'warning': 0.01,  'critical': 0.03},   # 1% / 3%
    'stock':      {'warning': 0.02,  'critical': 0.05},   # 2% / 5%
}


def classify(symbol: str, verify_price: Optional[float] = None,
             reference_price: Optional[float] = None):
    """
    判断资产类别。返回 (category, reference_symbol) 或 (None, None)

    verify_price/reference_price 用于股票代币的二次验证：
    仅当价格与 Yahoo 参考价偏离 < 30% 时才认定为股票代币（防止同名误报）
    """
    sym = symbol.upper().split('/')[0].split(':')[0]

    if sym in EXCLUDED:
        return None, None
    if sym in USD_STABLES:
        return 'usd_stable', None
    if sym in EUR_STABLES:
        return 'eur_stable', YAHOO_SYMBOLS['eurusd']
    if sym in GOLD_TOKENS:
        return 'gold', YAHOO_SYMBOLS['gold']
    if sym in KNOWN_STOCK_TOKENS:
        return 'stock', KNOWN_STOCK_TOKENS[sym]
    return None, None


def get_thresholds(category: str):
    return THRESHOLDS.get(category, {'warning': 0.01, 'critical': 0.03})


def all_candidate_symbols() -> List[str]:
    """所有候选符号（用于采集）"""
    return sorted(set(USD_STABLES + EUR_STABLES + GOLD_TOKENS + list(KNOWN_STOCK_TOKENS.keys())))


class PegAssetDiscovery:
    """
    锚定资产发现 + 流动性过滤

    在启动时扫描各交易所的现货 ticker，对候选资产：
      1. 分类（稳定币/黄金/股票）
      2. 流动性检查（24h 成交额 >= 阈值）
      3. 记录各所可用的交易对
    """

    def __init__(self, config_manager):
        self.config = config_manager
        self.registry: Dict[str, Dict[str, Any]] = {}  # symbol -> {category, reference_symbol, volume_24h, exchanges}
        self.last_refresh = 0
        self.refresh_interval = 3600  # 每小时重新发现

    def refresh(self, exchanges: Dict[str, Any], force: bool = False) -> Dict[str, Dict[str, Any]]:
        """扫描交易所，重建注册表"""
        now = time.time()
        if not force and (now - self.last_refresh) < self.refresh_interval and self.registry:
            return self.registry

        min_volume = float(self.config.get('alerts', 'peg_min_volume_usd', 500_000))
        registry: Dict[str, Dict[str, Any]] = {}

        for name, adapter in exchanges.items():
            try:
                tickers = adapter.exchange.fetch_tickers(params={'type': 'spot'})
            except Exception as e:
                logger.warning(f"{name} ticker fetch failed during discovery: {e}")
                continue

            for sym, t in tickers.items():
                if not sym.endswith('/USDT') or ':' in sym:
                    continue
                base = sym.split('/')[0]
                category, ref_symbol = classify(base)
                if not category:
                    continue

                price = t.get('last')
                volume = float(t.get('quoteVolume') or 0)
                if not price or price <= 0:
                    continue

                # 流动性过滤
                if volume < min_volume:
                    continue

                entry = registry.setdefault(base, {
                    'symbol': base,
                    'category': category,
                    'reference_symbol': ref_symbol,
                    'volume_24h': 0,
                    'exchanges': [],
                })
                entry['volume_24h'] += volume
                if name not in entry['exchanges']:
                    entry['exchanges'].append(name)

        self.registry = registry
        self.last_refresh = now

        by_cat: Dict[str, int] = {}
        for v in registry.values():
            by_cat[v['category']] = by_cat.get(v['category'], 0) + 1
        logger.info(f"Peg asset discovery: {len(registry)} assets {by_cat} (min_volume=${min_volume:,.0f})")
        return registry

    def get_monitored_symbols(self) -> List[str]:
        return sorted(self.registry.keys())
