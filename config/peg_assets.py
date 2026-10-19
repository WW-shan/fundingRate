"""
锚定资产监控注册表（v2）

基于 2026-10 官方调研（Binance/OKX/Bybit/Gate/Bitget 官方文档 + 发行商官网）：

六大 RWA 发行商：
  1. Binance bStock        — FSRA 监管，1:1 背书（Nest Trading 券商托管）
  2. Ondo Global Markets   — Total-Return Tracker，450+ 资产，$1.32B TVL
  3. Backed Finance (xStocks) — 瑞士 DLT 法案，1:1 背书，Kraken 生态
  4. Bitget rToken (Reality) — Alpaca 托管（FINRA/SIPC），500+ 资产
  5. Gate gStocks          — Gate 原生，1:1 全额储备
  6. OKX Unified Tokenized Stock — 分销 xStocks，转换为 share 计价

设计原则：
  - 按"资产"聚合（同一标的多个发行商版本归为一组），支持跨发行商价差比较
  - 流动性按"全市场总量"判定（避免单所量小被误杀）
  - 参考价来自 Yahoo Finance（黄金/原油/股票/指数）
"""

import time
from typing import Dict, List, Any, Optional
from loguru import logger

# ==================== 稳定币（锚定 1.0 USD） ====================
# 仅保留主流、流动性充足的
USD_STABLES = [
    'USDC', 'FDUSD', 'PYUSD', 'RLUSD', 'USD1', 'USDE',
    'USDS', 'USDG', 'USDD', 'XUSD', 'BUSD', 'DAI', 'BFUSD',
    'RWUSD', 'USDGO', 'USDTB',
]

# ==================== 欧元稳定币（锚定 EUR/USD） ====================
EUR_STABLES = ['EUR', 'EURI']

# ==================== 贵金属 / 大宗商品 ====================
COMMODITIES = {
    'XAUT': 'GC=F',      # Tether Gold（现货黄金）
    'PAXG': 'GC=F',      # Paxos Gold（现货黄金）
    'SLVON': 'SLV',      # Ondo 白银 ETF
    'IAUON': 'IAU',      # Ondo 黄金 ETF（iShares）
    'GLDX': 'GLD',       # xStocks 黄金 ETF
    'RSGOL': 'GC=F',     # Reality 黄金
    'RGOLD': 'GC=F',     # Reality 黄金
    'RFOIL': 'CL=F',     # Reality 原油（WTI）
}

# ==================== 股票代币（发行商 -> 标的映射） ====================
# 结构：{交易所符号: (发行商, Yahoo 标的代码)}
STOCK_TOKENS = {
    # ---- Binance bStock（B 后缀）----
    'AAPLB': ('bstock', 'AAPL'), 'TSLAB': ('bstock', 'TSLA'), 'NVDAB': ('bstock', 'NVDA'),
    'COINB': ('bstock', 'COIN'), 'MSTRB': ('bstock', 'MSTR'), 'HOODB': ('bstock', 'HOOD'),
    'AMZNB': ('bstock', 'AMZN'), 'GOOGLB': ('bstock', 'GOOGL'), 'METAB': ('bstock', 'META'),
    'MSFTB': ('bstock', 'MSFT'), 'SPYB': ('bstock', 'SPY'), 'QQQB': ('bstock', 'QQQ'),
    'PLTRB': ('bstock', 'PLTR'), 'INTCB': ('bstock', 'INTC'), 'AMDB': ('bstock', 'AMD'),
    'MUB': ('bstock', 'MU'), 'ORCLB': ('bstock', 'ORCL'), 'JPMB': ('bstock', 'JPM'),
    'GMEB': ('bstock', 'GME'), 'RDDTB': ('bstock', 'RDDT'), 'CRCLB': ('bstock', 'CRCL'),
    'NFLXB': ('bstock', 'NFLX'), 'DELLB': ('bstock', 'DELL'), 'IBMB': ('bstock', 'IBM'),
    'AVGOB': ('bstock', 'AVGO'), 'MRVLB': ('bstock', 'MRVL'), 'SMCIB': ('bstock', 'SMCI'),
    'COHRB': ('bstock', 'COHR'), 'SNDKB': ('bstock', 'SNDK'), 'WDCB': ('bstock', 'WDC'),
    'CRWVB': ('bstock', 'CRWV'), 'NBISB': ('bstock', 'NBIS'), 'SOXLB': ('bstock', 'SOXL'),
    'UVXYB': ('bstock', 'UVXY'), 'TQQQB': ('bstock', 'TQQQ'), 'SQQQB': ('bstock', 'SQQQ'),
    'SPCXB': ('bstock', 'SPCX'), 'BABAB': ('bstock', 'BABA'), 'PDDB': ('bstock', 'PDD'),
    'NOKB': ('bstock', 'NOK'), 'GSB': ('bstock', 'GS'), 'AMATB': ('bstock', 'AMAT'),
    'ASMLB': ('bstock', 'ASML'), 'ASTSB': ('bstock', 'ASTS'), 'BMNRB': ('bstock', 'BMNR'),
    'CBRSB': ('bstock', 'CBRS'), 'CRDOB': ('bstock', 'CRDO'), 'CRMB': ('bstock', 'CRM'),
    'CRWDB': ('bstock', 'CRWD'), 'DJTB': ('bstock', 'DJT'), 'EWYB': ('bstock', 'EWY'),
    'FLNCB': ('bstock', 'FLNC'), 'GLWB': ('bstock', 'GLW'), 'HIMSB': ('bstock', 'HIMS'),
    'HPEB': ('bstock', 'HPE'), 'INTWB': ('bstock', 'INTW'), 'IRENB': ('bstock', 'IREN'),
    'KORUB': ('bstock', 'KORU'), 'LITEB': ('bstock', 'LITE'), 'LLYB': ('bstock', 'LLY'),
    'MDBB': ('bstock', 'MDB'), 'MRNAB': ('bstock', 'MRNA'), 'MVLLB': ('bstock', 'MVLL'),
    'PYPLB': ('bstock', 'PYPL'), 'QCOMB': ('bstock', 'QCOM'), 'RKLBB': ('bstock', 'RKLB'),
    'SKHYB': ('bstock', 'SKHY'), 'SMHB': ('bstock', 'SMH'), 'SOXSB': ('bstock', 'SOXS'),
    'TSMB': ('bstock', 'TSM'), 'WENB': ('bstock', 'WEN'), 'ZMB': ('bstock', 'ZM'),
    'SHAZB': ('bstock', 'SHAZ'), 'SNXXB': ('bstock', 'SNXX'), 'DRAMB': ('bstock', 'DRAM'),
    # ---- Bybit xStocks（X 后缀）----
    'TSLAX': ('xstocks', 'TSLA'), 'NVDAX': ('xstocks', 'NVDA'), 'AAPLX': ('xstocks', 'AAPL'),
    'AMZNX': ('xstocks', 'AMZN'), 'GOOGLX': ('xstocks', 'GOOGL'), 'METAX': ('xstocks', 'META'),
    'COINX': ('xstocks', 'COIN'), 'HOODX': ('xstocks', 'HOOD'), 'SPCXX': ('xstocks', 'SPCX'),
    'MSTRX': ('xstocks', 'MSTR'), 'NFLXX': ('xstocks', 'NFLX'), 'MSFTX': ('xstocks', 'MSFT'),
    'QQQX': ('xstocks', 'QQQ'), 'SPYX': ('xstocks', 'SPY'), 'PEPX': ('xstocks', 'PEP'),
    'WMTX': ('xstocks', 'WMT'), 'CRCLX': ('xstocks', 'CRCL'),
    # ---- OKX Unified（X 前缀，分销 xStocks）----
    'XAAPL': ('okx', 'AAPL'), 'XTSLA': ('okx', 'TSLA'), 'XNVDA': ('okx', 'NVDA'),
    'XAMZN': ('okx', 'AMZN'), 'XGOOGL': ('okx', 'GOOGL'), 'XMETA': ('okx', 'META'),
    'XCOIN': ('okx', 'COIN'), 'XHOOD': ('okx', 'HOOD'), 'XMSTR': ('okx', 'MSTR'),
    'XMSFT': ('okx', 'MSFT'), 'XQQQ': ('okx', 'QQQ'), 'XSPY': ('okx', 'SPY'),
    'XPLTR': ('okx', 'PLTR'), 'XINTC': ('okx', 'INTC'), 'XAMD': ('okx', 'AMD'),
    'XAVGO': ('okx', 'AVGO'), 'XTSM': ('okx', 'TSM'), 'XSONY': ('okx', 'SONY'),
    'XCRCL': ('okx', 'CRCL'), 'XTQQQ': ('okx', 'TQQQ'), 'XSQQQ': ('okx', 'SQQQ'),
    'XIONQ': ('okx', 'IONQ'), 'XONDS': ('okx', 'ONDS'),
    # ---- Gate Ondo（ON 后缀）----
    'AAPLON': ('ondo', 'AAPL'), 'TSLAON': ('ondo', 'TSLA'), 'NVDAON': ('ondo', 'NVDA'),
    'AMZNON': ('ondo', 'AMZN'), 'GOOGLON': ('ondo', 'GOOGL'), 'METAON': ('ondo', 'META'),
    'COINON': ('ondo', 'COIN'), 'HOODON': ('ondo', 'HOOD'), 'MSTRON': ('ondo', 'MSTR'),
    'MSFTON': ('ondo', 'MSFT'), 'QQQON': ('ondo', 'QQQ'), 'SPYON': ('ondo', 'SPY'),
    'PLTRON': ('ondo', 'PLTR'), 'AMDON': ('ondo', 'AMD'), 'AVGOON': ('ondo', 'AVGO'),
    'NFLXON': ('ondo', 'NFLX'), 'BABAON': ('ondo', 'BABA'), 'CRCLON': ('ondo', 'CRCL'),
    'JPMON': ('ondo', 'JPM'), 'KOON': ('ondo', 'KO'), 'MCDON': ('ondo', 'MCD'),
    'PEPON': ('ondo', 'PEP'), 'PGON': ('ondo', 'PG'), 'LLYON': ('ondo', 'LLY'),
    'UNHON': ('ondo', 'UNH'), 'ACNON': ('ondo', 'ACN'), 'ABTON': ('ondo', 'ABT'),
    'CSCOON': ('ondo', 'CSCO'), 'CVXON': ('ondo', 'CVX'), 'LMTON': ('ondo', 'LMT'),
    'MAON': ('ondo', 'MA'), 'SBUXON': ('ondo', 'SBUX'), 'SKHYON': ('ondo', 'SKHY'),
    'SPCXON': ('ondo', 'SPCX'), 'TLTON': ('ondo', 'TLT'), 'IEFAON': ('ondo', 'IEFA'),
    'AGGON': ('ondo', 'AGG'), 'MAG7XON': ('ondo', 'MAG7'), 'YLD5ON': ('ondo', 'YLD5'),
    'YLD8ON': ('ondo', 'YLD8'), 'BTGOON': ('ondo', 'BTGO'), 'SBETON': ('ondo', 'SBET'),
    'BLKHION': ('ondo', 'BLKHI'), 'BLKDIGON': ('ondo', 'BLKDIG'), 'BLKGRWON': ('ondo', 'BLKGRW'),
    # ---- Gate gStocks（G 后缀）----
    'AAPLG': ('gstocks', 'AAPL'), 'TSLAG': ('gstocks', 'TSLA'), 'NVDAG': ('gstocks', 'NVDA'),
    'AMZNG': ('gstocks', 'AMZN'), 'GOOGLG': ('gstocks', 'GOOGL'), 'METAG': ('gstocks', 'META'),
    'COING': ('gstocks', 'COIN'), 'HOODG': ('gstocks', 'HOOD'), 'MSTRG': ('gstocks', 'MSTR'),
    'MSFTG': ('gstocks', 'MSFT'), 'QQQG': ('gstocks', 'QQQ'), 'SPYG': ('gstocks', 'SPY'),
    'PLTRG': ('gstocks', 'PLTR'), 'INTCG': ('gstocks', 'INTC'), 'AMDG': ('gstocks', 'AMD'),
    'AVGOG': ('gstocks', 'AVGO'), 'TSMG': ('gstocks', 'TSM'), 'CRCLG': ('gstocks', 'CRCL'),
    'JPMG': ('gstocks', 'JPM'), 'KOG': ('gstocks', 'KO'), 'PGG': ('gstocks', 'PG'),
    'LLYG': ('gstocks', 'LLY'), 'UNHG': ('gstocks', 'UNH'), 'CSCOG': ('gstocks', 'CSCO'),
    'CVXG': ('gstocks', 'CVX'), 'IBMG': ('gstocks', 'IBM'),
    'DELLG': ('gstocks', 'DELL'), 'MRVLG': ('gstocks', 'MRVL'), 'SNDKG': ('gstocks', 'SNDK'),
    'WDCG': ('gstocks', 'WDC'), 'CRWVG': ('gstocks', 'CRWV'), 'NBISG': ('gstocks', 'NBIS'),
    'SOXLG': ('gstocks', 'SOXL'), 'SOXSG': ('gstocks', 'SOXS'), 'TQQQG': ('gstocks', 'TQQQ'),
    'SPCXG': ('gstocks', 'SPCX'), 'AMATG': ('gstocks', 'AMAT'), 'BACG': ('gstocks', 'BAC'), 'BRKBG': ('gstocks', 'BRK-B'), 'CATG': ('gstocks', 'CAT'),
    'COSTG': ('gstocks', 'COST'), 'GLWG': ('gstocks', 'GLW'), 'GSG': ('gstocks', 'GS'),
    'HDG': ('gstocks', 'HD'), 'HSBCG': ('gstocks', 'HSBC'), 'IVVG': ('gstocks', 'IVV'),
    'JNJG': ('gstocks', 'JNJ'), 'KORUG': ('gstocks', 'KORU'), 'LITEG': ('gstocks', 'LITE'),
    'LRCXG': ('gstocks', 'LRCX'), 'MUG': ('gstocks', 'MU'), 'NFLXG': ('gstocks', 'NFLX'),
    'ORCLG': ('gstocks', 'ORCL'), 'QCOMG': ('gstocks', 'QCOM'), 'SAMSUNGG': ('gstocks', 'SAMSUNG'),
    'SKHYG': ('gstocks', 'SKHY'), 'SKHYNIXG': ('gstocks', 'SKHYNIX'), 'SMHG': ('gstocks', 'SMH'),
    'SNXXG': ('gstocks', 'SNXX'), 'TENCENTG': ('gstocks', 'TENCENT'), 'VOOG': ('gstocks', 'VOO'),
    'WMTG': ('gstocks', 'WMT'), 'XOMG': ('gstocks', 'XOM'), 'XTAG': ('gstocks', 'XTA'),
    'INTWG': ('gstocks', 'INTW'), 'MVLLG': ('gstocks', 'MVLL'), 'FLNCG': ('gstocks', 'FLNC'),
    'CBRSG': ('gstocks', 'CBRS'), 'EWYG': ('gstocks', 'EWY'), 'DRAMG': ('gstocks', 'DRAM'),
    'QNTG': ('gstocks', 'QNT'), 'AXTIG': ('gstocks', 'AXTI'), 'ABBVG': ('gstocks', 'ABBV'),
    'ZHIPUG': ('gstocks', 'ZHIPU'),
    # ---- Bitget rToken（R 前缀，Reality 发行）----
    'RAAPL': ('reality', 'AAPL'), 'RTSLA': ('reality', 'TSLA'), 'RNVDA': ('reality', 'NVDA'),
    'RAMZN': ('reality', 'AMZN'), 'RGOOGL': ('reality', 'GOOGL'), 'RMETA': ('reality', 'META'),
    'RCOIN': ('reality', 'COIN'), 'RHOOD': ('reality', 'HOOD'), 'RMSTR': ('reality', 'MSTR'),
    'RMSFT': ('reality', 'MSFT'), 'RQQQ': ('reality', 'QQQ'), 'RSPY': ('reality', 'SPY'),
    'RPLTR': ('reality', 'PLTR'), 'RINTC': ('reality', 'INTC'), 'RAMD': ('reality', 'AMD'),
    'RAVGO': ('reality', 'AVGO'), 'RTSM': ('reality', 'TSM'), 'RCRCL': ('reality', 'CRCL'),
    'RNFLX': ('reality', 'NFLX'), 'RBABA': ('reality', 'BABA'), 'RGS': ('reality', 'GS'),
    'RMU': ('reality', 'MU'), 'RORCL': ('reality', 'ORCL'), 'RIBM': ('reality', 'IBM'),
    'RMRVL': ('reality', 'MRVL'), 'RSMCI': ('reality', 'SMCI'), 'RSNDK': ('reality', 'SNDK'),
    'RWDC': ('reality', 'WDC'), 'RCRWV': ('reality', 'CRWV'), 'RNBIS': ('reality', 'NBIS'),
    'RSOXL': ('reality', 'SOXL'), 'RSOXS': ('reality', 'SOXS'), 'RTQQQ': ('reality', 'TQQQ'),
    'RSPCX': ('reality', 'SPCX'), 'RAMAT': ('reality', 'AMAT'), 'RASML': ('reality', 'ASML'),
    'RLLY': ('reality', 'LLY'), 'RUNH': ('reality', 'UNH'), 'RJPM': ('reality', 'JPM'),
    'RKO': ('reality', 'KO'), 'RPEP': ('reality', 'PEP'), 'RPG': ('reality', 'PG'),
    'RMCD': ('reality', 'MCD'), 'RSBUX': ('reality', 'SBUX'), 'RCSCO': ('reality', 'CSCO'),
    'RCVX': ('reality', 'CVX'), 'RLMT': ('reality', 'LMT'), 'RMA': ('reality', 'MA'),
    'RTLT': ('reality', 'TLT'), 'RIEFA': ('reality', 'IEFA'), 'RAGG': ('reality', 'AGG'),
    'RSPYG': ('reality', 'SPYG'), 'RBNDX': ('reality', 'BNDX'), 'RDOW': ('reality', 'DOW'),
    'RETON': ('reality', 'ETON'), 'RIONQ': ('reality', 'IONQ'),
    'RSON': ('reality', 'SON'), 'RSPXL': ('reality', 'SPXL'), 'RSPXS': ('reality', 'SPXS'),
    'RSPXU': ('reality', 'SPXU'), 'RUVXY': ('reality', 'UVXY'), 'RUDOW': ('reality', 'UDOW'),
    'RZION': ('reality', 'ZION'), 'RHOOD': ('reality', 'HOOD'), 'RIOND': ('reality', 'IOND'),
    'RIQQQ': ('reality', 'IQQQ'), 'RQQQI': ('reality', 'QQQI'), 'RQQQM': ('reality', 'QQQM'),
    'RSNDX': ('reality', 'SNDX'), 'RSPYI': ('reality', 'SPYI'), 'RSPYM': ('reality', 'SPYM'),
    'RSPYV': ('reality', 'SPYV'), 'RSQQQ': ('reality', 'SQQQ'), 'RTSMX': ('reality', 'TSMX'),
    'RVONG': ('reality', 'VONG'), 'RZION': ('reality', 'ZION'),
}

# ==================== 排除清单（人工确认的劣质/误报资产） ====================
EXCLUDED = {
    'BUCK',    # GME Mascot（meme 币，非锚定资产）
    'USTC',    # 已崩盘（-99.5%），无锚定意义
    'NEURO',   # 非稳定币
    'SUSD',    # 流动性枯竭，价格失真
    'USDEB',   # 非稳定币（价格 ~10.7）
    'CUSD',    # 流动性极差（$19/日）
    'CRVUSD',  # 流动性枯竭（$0/日）
    'GHO',     # 流动性差
    'FRAX',    # 已脱锚且流动性差（真实价格 ~0.26 说明已非稳定币）
    'TUSD',    # 流动性差（$53K 全市场）
    'USDP',    # 流动性差
    'AEUR',    # 流动性差
    'USDPT',   # 流动性差
    'USDSOLD', # 流动性差
    'RIRON',   # 映射验证失败（代币 $74 vs Disc Medicine $56.8，新上线未收敛）
}

# ==================== 参考价来源 ====================
YAHOO_SYMBOLS = {
    'gold': 'GC=F',       # 黄金期货
    'silver': 'SI=F',     # 白银期货
    'oil': 'CL=F',        # WTI 原油
    'eurusd': 'EURUSD=X', # 欧元兑美元
}

# ==================== 脱锚阈值 ====================
THRESHOLDS = {
    'usd_stable': {'warning': 0.003, 'critical': 0.01},   # 0.3% / 1%
    'eur_stable': {'warning': 0.005, 'critical': 0.02},   # 0.5% / 2%
    'commodity':  {'warning': 0.01,  'critical': 0.03},   # 1% / 3%
    'stock':      {'warning': 0.02,  'critical': 0.05},   # 2% / 5%
}

# 发行商显示名
ISSUER_LABELS = {
    'bstock': 'Binance bStock',
    'ondo': 'Ondo Global Markets',
    'xstocks': 'Backed xStocks',
    'okx': 'OKX Unified',
    'gstocks': 'Gate gStocks',
    'reality': 'Bitget Reality',
}


def classify(symbol: str):
    """
    判断资产类别与发行商
    返回 (category, reference_symbol, issuer) 或 (None, None, None)
    """
    sym = symbol.upper().split('/')[0].split(':')[0]

    if sym in EXCLUDED:
        return None, None, None
    if sym in USD_STABLES:
        return 'usd_stable', None, None
    if sym in EUR_STABLES:
        return 'eur_stable', YAHOO_SYMBOLS['eurusd'], None
    if sym in COMMODITIES:
        return 'commodity', COMMODITIES[sym], None
    if sym in STOCK_TOKENS:
        issuer, yahoo = STOCK_TOKENS[sym]
        return 'stock', yahoo, issuer
    return None, None, None


def get_thresholds(category: str):
    return THRESHOLDS.get(category, {'warning': 0.01, 'critical': 0.03})


def all_candidate_symbols():
    """所有候选符号"""
    return sorted(set(USD_STABLES + EUR_STABLES + list(COMMODITIES.keys()) + list(STOCK_TOKENS.keys())))


class PegAssetDiscovery:
    """
    锚定资产发现 + 流动性过滤（v2）

    改进：
      1. 流动性按【全市场总量】判定（同一资产在 5 所的总成交额），
         避免因单所量小而漏掉主流资产
      2. 按【标的】聚合（如 AAPL 的 6 个发行商版本归为一组），
         支持跨发行商价差比较
    """

    def __init__(self, config_manager):
        self.config = config_manager
        self.registry: Dict[str, Dict[str, Any]] = {}       # symbol -> {category, reference_symbol, issuer, volume_24h, exchanges}
        self.by_underlying: Dict[str, List[str]] = {}       # yahoo_symbol -> [symbols]（跨发行商分组）
        self.last_refresh = 0
        self.refresh_interval = 3600

    def refresh(self, exchanges: Dict[str, Any], force: bool = False) -> Dict[str, Dict[str, Any]]:
        """扫描交易所，重建注册表（流动性按全市场总量判定）"""
        now = time.time()
        if not force and (now - self.last_refresh) < self.refresh_interval and self.registry:
            return self.registry

        min_volume = float(self.config.get('alerts', 'peg_min_volume_usd', 500_000))

        # 第一遍：收集所有候选资产的报价与成交额
        candidates: Dict[str, Dict[str, Any]] = {}
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
                category, ref_symbol, issuer = classify(base)
                if not category:
                    continue

                price = t.get('last')
                volume = float(t.get('quoteVolume') or 0)
                if not price or price <= 0:
                    continue

                entry = candidates.setdefault(base, {
                    'symbol': base,
                    'category': category,
                    'reference_symbol': ref_symbol,
                    'issuer': issuer,
                    'volume_24h': 0,
                    'exchanges': [],
                    'prices': {},
                })
                entry['volume_24h'] += volume
                if name not in entry['exchanges']:
                    entry['exchanges'].append(name)
                entry['prices'][name] = float(price)

        # 第二遍：按全市场总量过滤
        registry = {sym: e for sym, e in candidates.items() if e['volume_24h'] >= min_volume}

        # 按标的聚合（跨发行商分组）
        by_underlying: Dict[str, List[str]] = {}
        for sym, e in registry.items():
            ref = e.get('reference_symbol')
            if ref:
                by_underlying.setdefault(ref, []).append(sym)

        self.registry = registry
        self.by_underlying = by_underlying
        self.last_refresh = now

        by_cat: Dict[str, int] = {}
        for v in registry.values():
            by_cat[v['category']] = by_cat.get(v['category'], 0) + 1
        multi_issuer = sum(1 for syms in by_underlying.values() if len(syms) > 1)
        logger.info(
            f"Peg asset discovery: {len(registry)} assets {by_cat} | "
            f"{len(by_underlying)} underlyings ({multi_issuer} with multi-issuer) | "
            f"min_volume=${min_volume:,.0f} (aggregate)"
        )
        return registry

    def get_monitored_symbols(self) -> List[str]:
        return sorted(self.registry.keys())

    def get_cross_issuer_groups(self) -> Dict[str, List[str]]:
        """返回有多个发行商版本的标的组（用于跨发行商价差比较）"""
        return {k: v for k, v in self.by_underlying.items() if len(v) > 1}
