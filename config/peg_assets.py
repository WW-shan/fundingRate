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
import threading
from typing import Dict, List, Any
from loguru import logger

# ==================== 稳定币（锚定 1.0 USD） ====================
# 仅保留主流、流动性充足的
USD_STABLES = [
    'USDT', 'USDC', 'FDUSD', 'PYUSD', 'RLUSD', 'USD1', 'USDE',
    'USDS', 'USDG', 'USDD', 'XUSD', 'BUSD', 'DAI', 'BFUSD',
    'RWUSD', 'USDGO', 'USDTB',
]

# 发行商（用于跨发行商价差；未知发行商用符号本身区分）
STABLE_ISSUERS = {
    'USDT': 'tether', 'USDC': 'circle', 'FDUSD': 'first_digital', 'PYUSD': 'paxos',
    'RLUSD': 'ripple', 'USD1': 'wlfi', 'USDE': 'ethena',
    'USDS': 'sky', 'USDG': 'paxos', 'USDD': 'tron', 'XUSD': 'stables_labs',
    'BUSD': 'paxos', 'DAI': 'sky', 'BFUSD': 'binance',
    'RWUSD': 'binance', 'USDGO': 'usdgo', 'USDTB': 'bridge',
}

# ==================== 欧元稳定币（锚定 EUR/USD） ====================
EUR_STABLES = ['EUR', 'EURI']
EUR_ISSUERS = {'EUR': 'eur', 'EURI': 'circle'}

# ==================== 贵金属 / 大宗商品 ====================
COMMODITIES = {
    'XAUT': 'GC=F',      # Tether Gold（黄金期货参考，含期现基差）
    'PAXG': 'GC=F',      # Paxos Gold
    'SLVON': 'SLV',      # Ondo 白银 ETF
    'IAUON': 'IAU',      # Ondo 黄金 ETF（iShares）
    'GLDX': 'GLD',       # xStocks 黄金 ETF
    'RSGOL': 'GC=F',     # Reality 黄金
    'RGOLD': 'GC=F',     # Reality 黄金
    'RFOIL': 'CL=F',     # Reality 原油（WTI）
}

COMMODITY_ISSUERS = {
    'XAUT': 'tether', 'PAXG': 'paxos', 'SLVON': 'ondo', 'IAUON': 'ondo',
    'GLDX': 'backed', 'RSGOL': 'reality', 'RGOLD': 'reality', 'RFOIL': 'reality',
}

# 代币对应的参考资产数量（份额比例）。RSGOL/RGOLD 约 0.01 盎司/枚（价格交叉验证）
COMMODITY_RATIOS = {
    'RSGOL': 0.01, 'RGOLD': 0.01,  # 约 0.01 盎司/枚
    'RFOIL': 0.2,                   # 约 0.2 桶 WTI/枚（流动性低，通常会被门槛过滤）
}


def get_reference_ratio(symbol: str) -> float:
    return COMMODITY_RATIOS.get(symbol.upper(), 1.0)

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
    'RZION': ('reality', 'ZION'), 'RIOND': ('reality', 'IOND'),
    'RIQQQ': ('reality', 'IQQQ'), 'RQQQI': ('reality', 'QQQI'), 'RQQQM': ('reality', 'QQQM'),
    'RSNDX': ('reality', 'SNDX'), 'RSPYI': ('reality', 'SPYI'), 'RSPYM': ('reality', 'SPYM'),
    'RSPYV': ('reality', 'SPYV'), 'RSQQQ': ('reality', 'SQQQ'), 'RTSMX': ('reality', 'TSMX'),
    'RVONG': ('reality', 'VONG'),
    # ---- Yahoo 参考价交叉验证补充（24h 成交额 ≥ $1M，映射偏差 ≤25%）----
    'RVOO': ('reality', 'VOO'), 'RSOXX': ('reality', 'SOXX'), 'REWY': ('reality', 'EWY'), 'RBE': ('reality', 'BE'),
    'RLITE': ('reality', 'LITE'), 'RSMH': ('reality', 'SMH'), 'RDELL': ('reality', 'DELL'), 'RSKHY': ('reality', 'SKHY'),
    'RISRG': ('reality', 'ISRG'), 'RMRK': ('reality', 'MRK'), 'RIWM': ('reality', 'IWM'), 'RKORU': ('reality', 'KORU'),
    'RSGOV': ('reality', 'SGOV'), 'RTRV': ('reality', 'TRV'), 'RHYG': ('reality', 'HYG'), 'RMRNA': ('reality', 'MRNA'),
    'RIBIT': ('reality', 'IBIT'), 'RIREN': ('reality', 'IREN'), 'RSTX': ('reality', 'STX'), 'RLRCX': ('reality', 'LRCX'),
    'RDRAM': ('reality', 'DRAM'), 'RWMT': ('reality', 'WMT'), 'RUSO': ('reality', 'USO'), 'RRKLB': ('reality', 'RKLB'),
    'RJNJ': ('reality', 'JNJ'), 'RECG': ('reality', 'ECG'), 'RKLAC': ('reality', 'KLAC'), 'RAAOI': ('reality', 'AAOI'),
    'RATKR': ('reality', 'ATKR'), 'RSNXX': ('reality', 'SNXX'), 'RCIEN': ('reality', 'CIEN'), 'RVXX': ('reality', 'VXX'),
    'RARM': ('reality', 'ARM'), 'RXLE': ('reality', 'XLE'), 'RFTNT': ('reality', 'FTNT'), 'RXBI': ('reality', 'XBI'),
    'RCIFR': ('reality', 'CIFR'), 'RIVV': ('reality', 'IVV'), 'RVTI': ('reality', 'VTI'), 'RCECO': ('reality', 'CECO'),
    'RASTS': ('reality', 'ASTS'), 'RMUU': ('reality', 'MUU'), 'RADBE': ('reality', 'ADBE'), 'RPODD': ('reality', 'PODD'),
    'RULS': ('reality', 'ULS'), 'RCRWD': ('reality', 'CRWD'), 'RCLF': ('reality', 'CLF'), 'RCRDO': ('reality', 'CRDO'),
    'RGLD': ('reality', 'GLD'), 'RALAB': ('reality', 'ALAB'), 'RBCO': ('reality', 'BCO'), 'RSNOW': ('reality', 'SNOW'),
    'RUBER': ('reality', 'UBER'), 'RNOK': ('reality', 'NOK'), 'RAXTI': ('reality', 'AXTI'), 'RXLK': ('reality', 'XLK'),
    'RCBRS': ('reality', 'CBRS'), 'RIDA': ('reality', 'IDA'), 'RCOHR': ('reality', 'COHR'), 'RETN': ('reality', 'ETN'),
    'RVRT': ('reality', 'VRT'), 'RRTX': ('reality', 'RTX'), 'RDIA': ('reality', 'DIA'), 'RQLD': ('reality', 'QLD'),
    'RESAB': ('reality', 'ESAB'), 'RSCHD': ('reality', 'SCHD'), 'RAMGN': ('reality', 'AMGN'), 'RAPLD': ('reality', 'APLD'),
    'RGEV': ('reality', 'GEV'), 'RNOW': ('reality', 'NOW'), 'RPANW': ('reality', 'PANW'), 'RCRM': ('reality', 'CRM'),
    'RTSLL': ('reality', 'TSLL'), 'RV': ('reality', 'V'), 'RNKE': ('reality', 'NKE'), 'RNATL': ('reality', 'NATL'),
    'RMTRN': ('reality', 'MTRN'), 'RNPO': ('reality', 'NPO'), 'RAPP': ('reality', 'APP'), 'RDDOG': ('reality', 'DDOG'),
    'RVXUS': ('reality', 'VXUS'), 'RGLW': ('reality', 'GLW'), 'RAWI': ('reality', 'AWI'), 'RPRM': ('reality', 'PRM'),
    'RTXN': ('reality', 'TXN'), 'RSLV': ('reality', 'SLV'), 'RABT': ('reality', 'ABT'), 'RSNDQ': ('reality', 'SNDQ'),
    'RIWS': ('reality', 'IWS'), 'RGDX': ('reality', 'GDX'), 'RUPRO': ('reality', 'UPRO'), 'RGE': ('reality', 'GE'),
    'RANET': ('reality', 'ANET'), 'RBOXX': ('reality', 'BOXX'), 'RSPCH': ('reality', 'SPCH'), 'RPSQ': ('reality', 'PSQ'),
    'RBMNR': ('reality', 'BMNR'), 'RHPE': ('reality', 'HPE'), 'RNU': ('reality', 'NU'), 'RJPST': ('reality', 'JPST'),
    'RBIL': ('reality', 'BIL'), 'RMPWR': ('reality', 'MPWR'), 'RWTM': ('reality', 'WTM'), 'RMPC': ('reality', 'MPC'),
    'RIYW': ('reality', 'IYW'), 'RJEPQ': ('reality', 'JEPQ'), 'REWZ': ('reality', 'EWZ'), 'RSOFI': ('reality', 'SOFI'),
    'RVLO': ('reality', 'VLO'), 'RPYPL': ('reality', 'PYPL'), 'RSHOP': ('reality', 'SHOP'), 'RSSO': ('reality', 'SSO'),
    'RCLS': ('reality', 'CLS'), 'RMSA': ('reality', 'MSA'), 'RXLF': ('reality', 'XLF'), 'RVGT': ('reality', 'VGT'),
    'RFIX': ('reality', 'FIX'), 'RACN': ('reality', 'ACN'), 'RIGV': ('reality', 'IGV'), 'RVALE': ('reality', 'VALE'),
    'RQCOM': ('reality', 'QCOM'), 'RESNT': ('reality', 'ESNT'), 'REFA': ('reality', 'EFA'), 'RBA': ('reality', 'BA'),
    'RSHEL': ('reality', 'SHEL'), 'RT': ('reality', 'T'), 'RXLV': ('reality', 'XLV'), 'RCOST': ('reality', 'COST'),
    'REEM': ('reality', 'EEM'), 'RNVDL': ('reality', 'NVDL'), 'RPFE': ('reality', 'PFE'), 'RNVO': ('reality', 'NVO'),
    'RIBOC': ('reality', 'IBOC'), 'RXLU': ('reality', 'XLU'), 'RTER': ('reality', 'TER'), 'RWULF': ('reality', 'WULF'),
    'RBAC': ('reality', 'BAC'), 'RAAL': ('reality', 'AAL'), 'RXLI': ('reality', 'XLI'), 'RINTU': ('reality', 'INTU'),
    'RSMTC': ('reality', 'SMTC'), 'RHYS': ('reality', 'HYS'), 'RSTM': ('reality', 'STM'), 'RAAOX': ('reality', 'AAOX'),
    'RGGOV': ('reality', 'GGOV'), 'RXOM': ('reality', 'XOM'), 'RASST': ('reality', 'ASST'), 'RTCBI': ('reality', 'TCBI'),
    'RBITX': ('reality', 'BITX'), 'RSFBS': ('reality', 'SFBS'), 'RGATX': ('reality', 'GATX'), 'RAPAM': ('reality', 'APAM'),
    'RQID': ('reality', 'QID'), 'RRAM': ('reality', 'RAM'), 'RXLRE': ('reality', 'XLRE'), 'ROXY': ('reality', 'OXY'),
    'RBP': ('reality', 'BP'), 'RCEG': ('reality', 'CEG'), 'RXLP': ('reality', 'XLP'), 'RNGVT': ('reality', 'NGVT'),
    'RSPMO': ('reality', 'SPMO'), 'RMSTU': ('reality', 'MSTU'), 'RAMDL': ('reality', 'AMDL'), 'RKWR': ('reality', 'KWR'),
    'RMAGS': ('reality', 'MAGS'), 'RRSP': ('reality', 'RSP'), 'RCMG': ('reality', 'CMG'), 'RAGNC': ('reality', 'AGNC'),
    'RSEI': ('reality', 'SEI'), 'RIEI': ('reality', 'IEI'), 'RTSEM': ('reality', 'TSEM'), 'RNET': ('reality', 'NET'),
    'RPSX': ('reality', 'PSX'), 'RNBHC': ('reality', 'NBHC'), 'RWDAY': ('reality', 'WDAY'), 'RABVX': ('reality', 'ABVX'),
    'RURI': ('reality', 'URI'), 'RMCHP': ('reality', 'MCHP'), 'RXOP': ('reality', 'XOP'), 'RLNG': ('reality', 'LNG'),
    'RPENG': ('reality', 'PENG'), 'RBMY': ('reality', 'BMY'), 'RTMF': ('reality', 'TMF'), 'RTECL': ('reality', 'TECL'),
    'RMANE': ('reality', 'MANE'), 'RRCL': ('reality', 'RCL'), 'RGLDM': ('reality', 'GLDM'), 'RGM': ('reality', 'GM'),
    'RREGN': ('reality', 'REGN'), 'RVOOG': ('reality', 'VOOG'), 'RSMA': ('reality', 'SMA'), 'RTLK': ('reality', 'TLK'),
    'RVST': ('reality', 'VST'), 'RSSPC': ('reality', 'SSPC'), 'RO': ('reality', 'O'), 'RTGT': ('reality', 'TGT'),
    'RWFC': ('reality', 'WFC'), 'RGMAB': ('reality', 'GMAB'), 'RMETU': ('reality', 'METU'), 'RGDXU': ('reality', 'GDXU'),
    'RHDB': ('reality', 'HDB'), 'RDVN': ('reality', 'DVN'), 'ROKTA': ('reality', 'OKTA'), 'RQNT': ('reality', 'QNT'),
    'RDBD': ('reality', 'DBD'), 'RNEM': ('reality', 'NEM'), 'RMXL': ('reality', 'MXL'), 'RFLEX': ('reality', 'FLEX'),
    'RFN': ('reality', 'FN'), 'RTTMI': ('reality', 'TTMI'), 'RMARA': ('reality', 'MARA'), 'RNTAP': ('reality', 'NTAP'),
    'RSTLA': ('reality', 'STLA'), 'RMELI': ('reality', 'MELI'), 'RLIN': ('reality', 'LIN'), 'RHCA': ('reality', 'HCA'),
    'RSHY': ('reality', 'SHY'), 'RFBTC': ('reality', 'FBTC'), 'RGILD': ('reality', 'GILD'), 'RTMUS': ('reality', 'TMUS'),
    'XSNDK': ('okx', 'SNDK'), 'XSPCX': ('okx', 'SPCX'), 'XSOXL': ('okx', 'SOXL'), 'AAOIB': ('bstock', 'AAOI'),
    'XSOXS': ('okx', 'SOXS'), 'XMU': ('okx', 'MU'), 'XSKHY': ('okx', 'SKHY'), 'BNCB': ('bstock', 'BNC'),
    'XLITE': ('okx', 'LITE'), 'XMRVL': ('okx', 'MRVL'), 'AXTIB': ('bstock', 'AXTI'),
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
    'commodity':  {'warning': 0.02,  'critical': 0.05},   # 2% / 5%（黄金期货 vs 现货存在基差）
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
        return 'usd_stable', 'USD', STABLE_ISSUERS.get(sym, sym.lower())
    if sym in EUR_STABLES:
        return 'eur_stable', YAHOO_SYMBOLS['eurusd'], EUR_ISSUERS.get(sym, sym.lower())
    if sym in COMMODITIES:
        return 'commodity', COMMODITIES[sym], COMMODITY_ISSUERS.get(sym, sym.lower())
    if sym in STOCK_TOKENS:
        issuer, yahoo = STOCK_TOKENS[sym]
        return 'stock', yahoo, issuer
    return None, None, None


def get_thresholds(category: str):
    return THRESHOLDS.get(category, {'warning': 0.01, 'critical': 0.03})


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
        self.registry: Dict[str, Dict[str, Any]] = {}       # symbol -> {category, reference_symbol, issuer, reference_ratio, volume_24h}
        self.by_underlying: Dict[str, List[str]] = {}       # yahoo_symbol -> [symbols]（跨发行商分组）
        self.last_refresh = 0
        self.refresh_interval = 3600
        self._refresh_lock = threading.Lock()

    def refresh(self, exchanges: Dict[str, Any], force: bool = False) -> Dict[str, Dict[str, Any]]:
        """扫描交易所，重建注册表（流动性按全市场总量判定）"""
        with self._refresh_lock:
            now = time.time()
            # 并发线程（锚定资产/参考价循环）可能同时触发发现；60 秒内复用结果
            if self.registry and (now - self.last_refresh) < 60:
                return self.registry
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
                        'reference_ratio': get_reference_ratio(base),
                        'volume_24h': 0,
                    })
                    entry['volume_24h'] += volume

                # USDT 是报价资产，没有 USDT/USDT 交易对；用 USDC/USDT 反算
                usdc_usdt = tickers.get('USDC/USDT')
                if usdc_usdt and usdc_usdt.get('last'):
                    try:
                        usdt_price = 1.0 / float(usdc_usdt['last'])
                    except (TypeError, ValueError, ZeroDivisionError):
                        usdt_price = None
                    if usdt_price and usdt_price > 0:
                        entry = candidates.setdefault('USDT', {
                            'symbol': 'USDT',
                            'category': 'usd_stable',
                            'reference_symbol': 'USD',
                            'issuer': STABLE_ISSUERS['USDT'],
                            'reference_ratio': 1.0,
                            'volume_24h': 0,
                        })
                        entry['volume_24h'] += float(usdc_usdt.get('quoteVolume') or 0)

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

    def get_cross_issuer_groups(self) -> Dict[str, List[str]]:
        """返回有多个发行商版本的标的组（用于跨发行商价差比较）"""
        return {k: v for k, v in self.by_underlying.items() if len(v) > 1}
