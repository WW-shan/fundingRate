# 资金费率数据准确性修复设计文档

> **文档状态**: 已审核（所有结论均有官方文档原文 / 线上 API 实测 / CCXT 4.5.42 源码三级证据支撑）
> **创建日期**: 2026-10-19
> **审计人**: Codex
> **影响范围**: `exchanges/`、`core/data_collector.py`、`core/opportunity_monitor.py`、`utils/calculator.py`、`database/db_manager.py`、`requirements.txt`

---

## 1. 背景与目标

系统当前通过 CCXT `fetch_funding_rate()` 采集 Binance / OKX / Bybit / Gate / Bitget 五家交易所的资金费率，用于跨所费率套利（策略1）、期现费率套利（策略2A）、基差套利（策略2B）、方向性费率策略（策略3）的机会发现与收益预估。

2026-10-19 的全链路审计发现：**采集层字段解析大体正确，但计算层存在"结算周期不归一化"和"费率语义混用"两类高严重度数据准确性问题**，会直接导致跨所套利机会误报、年化收益计算错误。本文档给出经逐细节审核的修复设计。

### 1.1 审计方法（可复核）

| 证据来源 | 覆盖交易所 | 备注 |
|---|---|---|
| 官方文档原文 | Binance（jina 镜像抓取 developers.binance.com）、OKX（okx.com/docs-v5）、Bybit（bybit-exchange.github.io）、Gate（gateio/gateapi-python 官方 SDK 文档）、Bitget（bitget.com/docs/catalog 新版文档） | 抓取时间 2026-10-19，本地存档于 %TEMP% |
| 线上 API 实测 | Gate（api.gateio.ws 直连）、OKX（经代理） | Binance fapi 返回地域限制无法实测 |
| CCXT 4.5.42 本地源码 | 全部 5 家 | 安装路径 `site-packages/ccxt/`，含官方响应样例 |

---

## 2. 官方数据源逐家核对结果（事实基线）

### 2.1 Binance USDⓈ-M 永续

官方文档原文（`GET /fapi/v1/premiumIndex`，"Mark Price" 章节）：

```
lastFundingRate  string  "This is the Latest funding rate"   ← 最近一期【已结算】费率
nextFundingTime  int64   "Next Funding Time"                  ← 下次结算时间(ms)
interestRate     string  "Interest Rate"
（无结算间隔字段，无预测费率字段）
```

官方间隔数据源：`GET /fapi/v1/fundingInfo`（"Get Funding Rate Info"）：

```
fundingIntervalHours       int64   例: 8
adjustedFundingRateCap     string  例: 0.02500000
adjustedFundingRateFloor   string
```

**关键官方措辞**：*"Query funding rate info for symbols that had FundingRateCap/FundingRateFloor / fundingIntervalHours adjustment"* —— 该接口**只返回发生过调整的币种**，未返回的币种按默认 8h 处理。IP Weight = 0（与 fundingRate 共享 500/5min/IP），一次调用返回全部币种。

历史费率接口 `GET /fapi/v1/fundingRate` 官方说明：*"If startTime and endTime are not sent, the most recent limit records are returned. In ascending order."*

**语义裁定**：Binance 无法通过公开 API 获得下一期预测费率，`lastFundingRate` 是上一期实际结算值。与其他四家的"下一期待收取费率"**语义不同**。

### 2.2 OKX

官方文档原文（`GET /api/v5/public/funding-rate`，"Get funding rate"）：

```
fundingRate      "Predicted funding rate for the upcoming settlement period.
                  Sign: positive = long positions pay short positions at the next fundingTime...
                  This is a forecast — the final settled rate may differ.
                  See settFundingRate for the last settled rate."
fundingTime      即将结算 fundingRate 的时间(ms)
nextFundingRate  method=current_period 时为 ""（官方已注明 no longer supported）
settFundingRate  上一期已结算费率
```

官方对间隔的明确说明：*"the funding rate collection frequency, currently set at 8 hours, may be adjusted to higher frequencies such as 6 hours, 4 hours, 2 hours, or 1 hour. Thus, users should focus on the difference between `fundingTime` and `nextFundingTime` fields to determine the funding fee interval of a contract."*

**线上实测**（2026-10-19，BTC-USDT-SWAP）：
```json
{"fundingRate":"0.0000606958161039","fundingTime":"1792396800000",
 "nextFundingTime":"1792425600000","method":"current_period",
 "nextFundingRate":"","settFundingRate":"0.0000687705707140","settState":"settled"}
```
间隔 = 1792425600000 − 1792396800000 = 28800000ms = 8h ✓（与 CCXT 的 `nextFundingTime - fundingTime` 计算方式一致，即官方推荐做法）

**语义裁定**：`fundingRate` 是**下一期预测费率**，可直接作为预期收益依据（注意其本身是预测值）。

### 2.3 Bybit

官方文档原文（`GET /v5/market/tickers`，linear）：

```
fundingRate          "Funding rate"（将在 nextFundingTime 收取的当前费率）
nextFundingTime      "Next funding time (ms)"
fundingIntervalHour  "Funding interval hour. This value currently only supports whole hours"
```

另有 `GET /v5/market/instruments-info`：`fundingInterval` integer，**单位为分钟**（CCXT 4.5.42 使用该字段，`fundingInterval/60` 转小时）。

**语义裁定**：下一期待收取费率。间隔有**两个官方来源**（tickers 的 `fundingIntervalHour` 更直接，instruments-info 的 `fundingInterval` 分钟级），两者一致时即可信。

### 2.4 Gate.io

官方 SDK 文档原文（gateapi-python `Contract.md`）：

```
funding_rate           "Current funding rate"
funding_interval       "Funding application interval, unit in seconds"
funding_next_apply     "Next funding time"（float，unix 秒）
funding_rate_indicative 下一期指示性费率
```

**线上实测**（2026-10-19，BTC_USDT）：
```json
{"funding_rate":"0.000059","funding_interval":28800,"funding_next_apply":1792396800,
 "funding_rate_indicative":"0.000059","funding_rate_limit":"0.003"}
```
`funding_next_apply` 1792396800 = 2026-10-19 16:00 UTC，与 Gate 00/08/16 UTC 结算节奏一致 ✓

**语义裁定**：下一期待收取费率。`funding_interval` 单位是**秒**（代码已正确按秒处理）。`funding_rate_indicative` 是真正的"预测费率"字段，当前代码未使用。

### 2.5 Bitget

官方新版文档原文（bitget.com/docs/catalog/market/derivatives，`GET /api/v3/market/current-fund-rate`，v2 字段名相同）：

```
fundingRate          "Current funding rate. In decimal form, e.g. 0.000088 represents 0.0088%"
fundingRateInterval  "Funding rate settlement period, unit: hour"
nextUpdate           "Next update time of the funding rate. Unix millisecond timestamp"
```

注：Bitget 旧文档站 /api-doc/ 已整体 301/404 迁移至 /docs/，Classic 账户进入维护模式，字段语义与 v2 一致。

**语义裁定**：下一期待收取费率，间隔单位**小时**。

### 2.6 五家语义对照总表

| 交易所 | `fundingRate` 语义 | 下次结算时间字段 | 官方间隔字段 | 间隔单位 | 预测费率可用？ |
|---|---|---|---|---|---|
| Binance | **上一期已结算** | `nextFundingTime` | `fundingInfo.fundingIntervalHours`（仅调整过的币种，缺省=8h） | 小时 | ❌ 官方不提供 |
| OKX | 下一期**预测** | `fundingTime` | `nextFundingTime − fundingTime`（官方推荐） | 差值 ms | fundingRate 本身即预测 |
| Bybit | 下一期待收取 | `nextFundingTime` | tickers `fundingIntervalHour` / instruments `fundingInterval` | 小时 / 分钟 | fundingRate 本身即 |
| Gate | 下一期待收取 | `funding_next_apply`(秒) | `funding_interval` | **秒** | `funding_rate_indicative` |
| Bitget | 下一期待收取 | `nextUpdate` | `fundingRateInterval` | 小时 | fundingRate 本身即 |

---

## 3. 问题清单（按严重度排序）

### P1【高】跨所策略结算周期硬编码 8h，完全无视官方逐币种周期

**位置**：`core/opportunity_monitor.py:52-64` `_get_funding_frequency()`

```python
def _get_funding_frequency(self, long_data: Dict, short_data: Dict) -> int:
    # 尝试从next_funding_time计算间隔（需要两次数据才能计算间隔）
    # 这里暂不实现...
    # 简化版本：默认8小时
    return 8
```

函数注释中还写着"Gate/OKX/Binance/Bybit/Bitget 都是 8 小时"，与官方事实不符：
- Binance 自 2024 年末起大量山寨永续调整为 4h（`fundingInfo` 可查），极端行情有 1h
- OKX 官方明示可动态调整为 6/4/2/1h
- Bybit / Bitget / Gate 均有逐币种 1/2/4/8h 周期

**影响**（`opportunity_monitor.py:279-283`）：
- `annual_funding_diff = funding_diff * (24/8) * 365` —— 两腿均为 4h 币种时年化收益**少算一半**；一腿 4h 一腿 8h 时错得更多
- 讽刺的是，同一文件 `:66-100` 的 `_get_funding_frequency_single()`（策略2A/3 使用）已正确读取 `data['funding_interval']`，只有跨所这条被遗漏

### P2【高】跨所费率差未做周期归一化

**位置**：`core/opportunity_monitor.py:273-275`（阈值配置取自 `:255` 的 `s1_min_funding_diff`）与 `utils/calculator.py:calculate_cross_exchange_funding_profit()`

```python
funding_diff = short_data['funding_rate'] - long_data['funding_rate']
if funding_diff < min_funding_diff:  # 0.0005 阈值直接比较
```

两腿费率若对应不同结算周期（如 Binance 某 4h 币 vs Bitget 8h），单期费率的直接相减在数学上是错误的——0.01%/4h 的盈利能力是 0.01%/8h 的 2 倍。`funding_income = position_size * diff`（calculator.py）同样假设两腿同步同周期结算。

### P3【高】Binance 费率语义与其他四家相反，跨所比较混用

Binance `lastFundingRate` 是**上一期已结算**费率（官方："This is the Latest funding rate"），其余四家是**下一期待收取/预测**费率。跨所 `funding_diff` 实际在比较"Binance 的过去"与"别家的未来"。费率平稳期误差小，但费率翻转时会产生虚假机会——而费率翻转恰恰是跨所套利最想捕捉的场景。

### P4【中】DB 回退路径丢失 funding_interval

**位置**（同一缺陷存在于两处 DB 回退路径）：

1. `core/opportunity_monitor.py:126`（`_load_market_data_from_db`）SELECT 漏列，合并逻辑在 `:166-168`
2. `core/data_collector.py:205`（采集器自身的启动预热加载）SELECT 漏列，合并逻辑在 `:246-249`

两处 SQL 均为：

```sql
SELECT exchange, symbol, funding_rate, next_funding_time   -- 缺 funding_interval
FROM funding_rates WHERE timestamp > ? ...
```

`funding_rates` 表有 `funding_interval` 列（`database/db_manager.py` schema 已确认），采集端也正常写入（`data_collector.py:450-460` 的 INSERT），但两条 DB 回退路径都**漏查该列**。后果：系统重启后、内存数据尚未重建期间，策略2A/3 的 `_get_funding_frequency_single()` 对所有 4h 币种按 8h 年化，收益少算一半；跨所策略的两腿周期也无从读取。

### P5【中】`predicted_rate` 字段形同虚设

**位置**：`exchanges/base_exchange.py` `get_funding_rate()` 返回处

```python
'predicted_rate': funding.get('indicativeRate') or funding.get('fundingRate'),
```

CCXT FundingRate 规范结构不存在 `indicativeRate` 键（已通读 5 家 `parse_funding_rate` 源码确认），因此 `predicted_rate` 永远等于 `funding_rate`。真正可用的预测数据被丢弃：Gate 的 `info.funding_rate_indicative`、OKX 的 `settFundingRate`（上一期实际值，可用于预测值质量评估）。

### P6【中】ccxt 版本钉死与实际运行不一致

`requirements.txt` 钉 `ccxt==4.2.5`，实际安装运行 `4.5.42`。FundingRate 结构的 `interval` 规范化字段为 4.3+ 引入。全新部署装 4.2.5 时，`funding.get('interval')` 恒为 None，Bybit/Bitget/Gate/OKX 的间隔获取全部退化为 info 兜底或失效，行为与本设计验证的环境不一致。

### P7【低】Binance 间隔获取方式效率差且非官方推荐

当前兜底链路：`premiumIndex`（无间隔）→ info 各字段（无）→ **`fetch_funding_rate_history(limit=2)` 实测推算**。该兜底逻辑本身正确（官方：不传时间返回最近记录，升序，`abs()` 差值容错），但导致 Binance 每币种每 300s 采集周期发 2 次请求（350+ 币种 × 10 并发线程池）。官方提供的 `fundingInfo`（weight=0、一次全量）未被使用。

### P8【低】`fetch_funding_rate_history(limit=2)` 的边界风险

仅在结算频率调整过渡期，最近两条历史的间隔可能反映旧周期。验证范围 `1h ≤ interval ≤ 24h` 合理，保留但降为兜底。

### P9【高】"识别最高 APR"的目标在当前排序链路上无法实现

**位置**：`core/opportunity_monitor.py:235`（全局唯一排序）+ `utils/calculator.py`

```python
self.opportunities = sorted(new_opportunities, key=lambda x: x.get('expected_return_pct', 0), reverse=True)
```

`expected_return_pct` 在三个策略中分别是**单期净收益**（策略1/2A）和**7 天估算**（策略3），时间口径不同却混排在同一列表。更关键的是：单期口径在结算间隔不同（4h/8h/1h）时与 APR 排序结果不一致：

```
币种A @ 8h间隔: 0.03%/期 → APR = 0.03% × 3 × 365 = 32.85%
币种B @ 4h间隔: 0.02%/期 → APR = 0.02% × 6 × 365 = 43.80%
单期排序: A > B ❌    APR排序: B > A ✓
```

且手续费（~0.2-0.4%）远大于单期费率（~0.05%），`expected_return_pct` 普遍为深度负值，排序实际退化为按单期原始费率排序，4h 币种被系统性压低。

前端 `opportunities.html:580-582` 的"资金费率（年化）"仅为展示字段，不参与排序；策略1 的年化值还受 P1 影响本身就是错的。

### P10【中】策略3 预期收益单位 bug（低估 100 倍）

**位置**：`core/opportunity_monitor.py:653`

```python
'expected_return': position_size * (net_return_pct / 100),  # net_return_pct 已是小数，/100 多余
```

`net_return_pct`（`:625`）= `abs(funding_rate) * times_per_day * holding_days - fees_pct`，各项均为小数（如 0.0085），变量名 `_pct` 误导。10000 USDT 仓位显示预期收益 0.85 USDT，实际应为 85 USDT。

### 关联问题（不在本文档修复范围，仅记录）

`data_collector.py:122` `_cache_trading_fees()` 对 `get_trading_fees(symbol)` 传入**现货 symbol**，取出的是现货市场费率，却被同时当作现货+期货手续费参与成本计算（期货 taker 通常 0.05% vs 现货 0.1%），且为公共费率而非账户实际费率档。建议另开文档处理。

---

## 4. 修复设计

### 4.1 设计原则

1. **官方字段优先**：间隔一律来自各所官方字段（§2.6 表），硬编码 8h 仅作为最终兜底
2. **归一化后才比较**：所有跨所费率比较、阈值判断、年化计算，先将单期费率换算为统一年化口径
3. **语义显式化**：每个费率携带 `rate_semantics` 元数据（`upcoming` / `last_settled`），消费方不得无声混用
4. **向后兼容**：数据结构只增字段不改旧字段语义；DB schema 不变（`funding_interval` 列已存在）

### 4.2 采集层改造（`exchanges/base_exchange.py`）

#### 4.2.1 `get_funding_rate()` 返回值扩展

```python
return {
    'funding_rate': funding.get('fundingRate'),
    'next_funding_time': funding.get('fundingTimestamp'),
    'funding_interval': funding_interval,          # 保持现状，毫秒
    # ---- 新增 ----
    'rate_semantics': self.funding_rate_semantics,  # 各适配器类属性声明，见 4.2.2
    'predicted_rate': predicted_rate,               # 修复后的真实预测值，见 4.2.3
    'last_settled_rate': last_settled_rate,         # 上一期已结算费率（若官方提供）
}
```

#### 4.2.2 各适配器声明费率语义（类属性）

| 适配器 | `funding_rate_semantics` | 依据 |
|---|---|---|
| BinanceAdapter | `'last_settled'` | 官方：`lastFundingRate` = "Latest funding rate" |
| OKXAdapter | `'predicted_upcoming'` | 官方：`fundingRate` = "Predicted funding rate for the upcoming settlement period" |
| BybitAdapter | `'upcoming'` | 官方 tickers 字段定义 |
| GateAdapter | `'upcoming'` | 官方："Current funding rate"，在 `funding_next_apply` 收取 |
| BitgetAdapter | `'upcoming'` | 官方："Current funding rate"，在 `nextUpdate` 收取 |

#### 4.2.3 `predicted_rate` / `last_settled_rate` 真实化

- **Gate**：`predicted_rate = info.funding_rate_indicative`；`last_settled_rate = funding_rate`（current 即将在下次结算收取的值，结算后即成为已结算值）
- **OKX**：`predicted_rate = fundingRate`（本身即预测）；`last_settled_rate = info.settFundingRate`（官方字段，current_period 模式下可用）
- **Bybit / Bitget**：`predicted_rate = funding_rate`（本身即下期费率）；`last_settled_rate = None`（官方单次请求不提供，需要历史接口，不在采集主链路增加请求）
- **Binance**：`last_settled_rate = funding_rate`（=lastFundingRate）；`predicted_rate = None`（官方不提供）。可选增强（默认关闭，配置项 `global.estimate_binance_predicted_rate=false`）：用 premiumIndex 的 `markPrice/indexPrice` 溢价按 Binance 公式粗估预测费率：`estimated = clamp(premium + interestRate, floor, cap)`，其中 cap/floor 来自 `fundingInfo`。**该估算未经官方背书，仅作为排序参考，不参与开仓决策**

#### 4.2.4 Binance 间隔获取改用官方 fundingInfo（解决 P7）

在 `BinanceAdapter` 新增批量间隔缓存：

```python
class BinanceAdapter(BaseExchange):
    funding_rate_semantics = 'last_settled'
    _funding_intervals_cache = {}   # {base_symbol: hours}
    _intervals_cache_ts = 0
    _INTERVALS_TTL = 3600           # 每小时刷新一次（间隔调整是低频事件）

    def _refresh_funding_intervals(self):
        # 底层 GET /fapi/v1/fundingInfo，weight=0，一次全量
        # 官方注意：只返回调整过的币种 → 未覆盖的币种保持 8h 默认
        if time.time() - self._intervals_cache_ts < self._INTERVALS_TTL:
            return
        infos = self.exchange.fetch_funding_intervals()  # CCXT 已封装
        for symbol, data in infos.items():
            ... # 解析 '8h'→8，存入 _funding_intervals_cache[base_symbol]
        self._intervals_cache_ts = time.time()
```

`get_funding_rate()` 中间隔解析顺序调整为：
1. CCXT `interval` 规范化字段（OKX/Bybit/Bitget/Gate 命中）
2. Binance：查 `_funding_intervals_cache`，未命中 → 8h
3. info 字段兜底（保留现有逻辑，兼容 ccxt 降级）
4. `fetch_funding_rate_history(limit=2)` 实测推算（保留为最终兜底，降权使用）

效果：Binance 每周期请求数从 2N 降为 N（+ 每小时 1 次批量）。

### 4.3 计算层改造（`core/opportunity_monitor.py` + `utils/calculator.py`）

#### 4.3.1 修复 `_get_funding_frequency()`（解决 P1）

```python
def _get_funding_frequency(self, long_data: Dict, short_data: Dict) -> Tuple[int, int]:
    """返回 (long_leg_hours, short_leg_hours)，各自独立读取官方间隔"""
    long_hours = self._get_funding_frequency_single(long_data)
    short_hours = self._get_funding_frequency_single(short_data)
    return long_hours, short_hours
```

**注意签名变更**（返回单值 → 二元组），调用点仅 `opportunity_monitor.py:279` 一处，同步修改。`_get_funding_frequency_single()` 逻辑不变（已正确），但删除其中"按交易所硬编码 8h"的注释段与误导性注释。

#### 4.3.2 跨所比较归一化（解决 P2）

`opportunity_monitor.py:270-283` 修改：

```python
long_hours, short_hours = self._get_funding_frequency(long_data, short_data)

# 归一化：两腿各自换算为年化费率后再求差
long_annual = long_data['funding_rate'] * (24 / long_hours) * 365
short_annual = short_data['funding_rate'] * (24 / short_hours) * 365
annual_funding_diff = short_annual - long_annual

# 阈值比较同样用年化口径：min_funding_diff(单期8h口径0.0005) → 换算为年化阈值
# 0.0005 * 3 * 365 = 0.5475（54.75% 年化），保持与既有配置语义等价
min_annual_diff = pair_config.get('s1_min_funding_diff', 0.0005) * 3 * 365
if annual_funding_diff < min_annual_diff:
    continue

# 新增元数据，供前端/日志展示两腿周期不一致的提示
interval_mismatch = (long_hours != short_hours)
```

`calculate_cross_exchange_funding_profit()`（calculator.py）签名增加两腿周期参数：

```python
def calculate_cross_exchange_funding_profit(
    position_size, short_rate, long_rate,
    short_hours: int, long_hours: int,   # 新增
    ...):
    # 单期收益改为"一个共同时间窗内的期望收益"：以 24h 为统一窗口
    funding_income_24h = position_size * (
        short_rate * (24 / short_hours) - long_rate * (24 / long_hours)
    )
```

机会字典新增字段：`long_funding_hours`、`short_funding_hours`、`interval_mismatch`（向后兼容，仅新增）。`funding_frequency_hours`/`times_per_day` 字段在跨所机会中废弃（两腿不同周期时单值无意义），保留一个周期避免前端 breaking change，标记 deprecated。

#### 4.3.3 费率语义混用防护（解决 P3）

跨所机会计算时检查两腿语义：

```python
semantics_warn = (
    long_data.get('rate_semantics') == 'last_settled'
    or short_data.get('rate_semantics') == 'last_settled'
)
```

- 任一腿为 `last_settled`（即含 Binance）时：机会字典标记 `'rate_semantics_mixed': True`，前端/TG 推送展示"Binance 费率为上期实际值，注意翻转风险"
- 风控联动（`risk_manager.check_pre_trade_risk` 无需改签名）：当 `interval_mismatch or rate_semantics_mixed` 时，将该机会的 `risk_level` 从 `low` 上调为 `medium`，使其在 auto 模式下不直接执行（`strategy_executor.submit_opportunity` 现有逻辑：`auto + low` 才自动执行）——**利用现有机制，零新增风控代码**

策略2A/2B/3 为单所策略，无跨所语义混用问题；策略2A 使用 Binance 数据时同理标注即可。

### 4.4 数据链路修复

#### 4.4.1 DB 回退查询补列（解决 P4）

两处 SELECT 同步补列（`opportunity_monitor.py:126` 与 `data_collector.py:205`）：

```sql
SELECT exchange, symbol, funding_rate, next_funding_time, funding_interval
FROM funding_rates WHERE timestamp > ? ORDER BY timestamp DESC
```

两处合并逻辑（`opportunity_monitor.py:166-168`、`data_collector.py:246-249`）同步补充 `'funding_interval': row['funding_interval']`。`predicted_rate`、`rate_semantics` 不入库（瞬时派生数据，避免 schema 变更），内存链路完整保留。

#### 4.4.2 ccxt 版本统一（解决 P6）

`requirements.txt`：`ccxt==4.2.5` → `ccxt>=4.5.42,<5.0`。理由：
- `interval` 规范化字段为 4.3+ 引入，本设计 4.2.4 依赖它
- 大版本 5 未发布前锁上界防 breaking
- 部署文档补充：`pip install -U -r requirements.txt` 后需重启系统

### 4.5 配置项变更（三层配置体系内，均支持热更新）

| 配置 | category | 默认值 | 说明 |
|---|---|---|---|
| `binance_interval_cache_ttl` | global | 3600 | fundingInfo 缓存秒数 |
| `estimate_binance_predicted_rate` | global | false | Binance 预测费率粗估开关（默认关） |
| `s1_min_annual_funding_diff` | strategy1 | 0.5475 | 新增年化阈值（与旧单期 0.0005 等价），旧配置键保留兼容一季 |

### 4.6 APR 排行链路（解决 P9/P10，直接支撑"识别最高 APR 费率"的核心目的）

#### 4.6.1 统一 APR 字段（每个机会必带）

```python
# 策略2A/3（单所）：
funding_apr = funding_rate * (24 / interval_hours) * 365        # interval_hours 来自官方字段

# 策略1（跨所，两腿独立周期）：
funding_diff_apr = short_rate * (24/short_hours) * 365 - long_rate * (24/long_hours) * 365
```

全部采用**线性 APR**（不做复利），与交易所业界展示口径一致（Binance/Bybit 官方页面的年化均为线性换算）。

#### 4.6.2 排序键分层

- `OpportunityMonitor` 内存列表改为支持多键排序，默认键改为 `funding_apr`（单所）/ `funding_diff_apr`（跨所）——**纯费率维度，不含手续费**，回答"哪个币种费率最高"
- `expected_return_pct` 保留为第二排序键（"值不值得开仓"维度，含成本），各策略统一为**单期口径**（策略3 的 7 天估算移入 `details`，并修复 P10 的 /100）
- Web API `/api/opportunities` 增加 `?sort=apr|net|score` 参数，默认 `apr`
- 前端机会监控页增加"APR 排行"Tab，按 `funding_apr`/`funding_diff_apr` 降序，并排显示"每 N 小时结算 · 每日 X 次"（间隔信息必须可见，否则 APR 数字失去上下文）

#### 4.6.3 全市场纯费率榜（新增轻量功能）

新增 `GET /api/funding-apr-leaderboard`：不经策略过滤器，直接从最新 `funding_rates` 快照计算所有币种×所有交易所的 APR 并排行（Top N=50，可配置）。这是"识别出 APR 最高的资金费率"最直接的实现——策略机会列表只是它的子集（还要过阈值/成本/基差过滤），两者互补：

- **APR 榜**：发现"哪里的费率最极端"（纯数据，零策略假设）
- **机会列表**：回答"哪个机会扣完成本还值得做"

---

## 5. 验证方案

### 5.1 单元测试（新建 `tests/test_funding_accuracy.py`，pytest）

| 用例 | 断言 |
|---|---|
| Binance 适配器 fixture（premiumIndex 样例 + fundingInfo 样例） | `rate_semantics='last_settled'`；4h 币种 interval=14400000ms；未在 fundingInfo 中的币种=8h |
| OKX fixture（§2.2 实测 JSON） | interval=28800000ms；`last_settled_rate=0.0000687705707140` |
| Gate fixture（§2.4 实测 JSON） | 秒→ms 转换正确；`predicted_rate=funding_rate_indicative` |
| 跨所归一化：long 腿 8h 0.01%，short 腿 4h 0.02% | `annual_funding_diff = 0.02*6*365 - 0.01*3*365 = 32.85` |
| 周期不一致的机会 | `interval_mismatch=True` 且 risk_level 上调 |
| DB 回退加载 | 内存与 DB 两条路径得到的 `funding_interval` 一致 |
| APR 排序正确性：A=0.03%/8h vs B=0.02%/4h | B 排在 A 前（APR 43.8% > 32.85%） |
| 策略3 P10 | 10000 USDT、费率 0.05%/8h、7 天、默认手续费 → `expected_return` ≈ 85 USDT（0.0005×3×7−0.002=0.0085；当前代码错误输出 0.85） |

### 5.2 集成验证（线上 dry-run，`ENABLE_TRADING=False`）

1. 启动系统，对同一币种（BTC/USDT、ETH/USDT + 一个 4h 山寨币）抓取 5 所数据
2. 编写核对脚本 `scripts/verify_funding_data.py`：直接调 5 所官方公开端点（本文档 §2 已验证的 URL），与系统内存 `market_data` 及 `funding_rates` 表最新行逐字段比对：费率值、next_funding_time、interval
3. 容差：费率值精确相等；时间戳 ±60s；interval 精确相等
4. 连续比对 3 个采集周期（15 分钟）全部通过为验收标准

### 5.3 回归验证

- 策略2A/3 的年化收益在修复前后对 8h 币种数值不变（回归基线）
- 前端机会监控页确认 `funding_frequency_hours` 旧字段仍存在（deprecated 但有值）

---

## 6. 实施任务分解

| Task | 文件 | 内容 | 依赖 |
|---|---|---|---|
| 1 | `exchanges/*.py` | 各适配器加 `funding_rate_semantics`；Binance 加 fundingInfo 缓存 | — |
| 2 | `exchanges/base_exchange.py` | `get_funding_rate()` 返回扩展 + 间隔解析顺序调整 | Task 1 |
| 3 | `core/opportunity_monitor.py`、`core/data_collector.py` | `_get_funding_frequency` 签名变更 + 跨所归一化 + 语义防护 + 两处 DB SELECT 补列 | Task 2 |
| 4 | `utils/calculator.py` | `calculate_cross_exchange_funding_profit` 增加周期参数 | 可与 3 并行 |
| 5 | `requirements.txt` | ccxt 版本放宽 | — |
| 6 | `tests/test_funding_accuracy.py` | §5.1 全部用例 | Task 1-4 |
| 7 | `scripts/verify_funding_data.py` | §5.2 核对脚本 | Task 2 |
| 8 | `core/opportunity_monitor.py`、`web/routes/api.py`、`web/templates/opportunities.html` | APR 统一字段 + 排序分层 + APR 榜 API/前端 Tab + P10 单位修复 | Task 3 |

预估改动量：约 200 行新增/修改，不涉及 DB schema 变更，不需要数据迁移。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| `_get_funding_frequency` 签名变更遗漏调用点 | 全仓搜索确认仅 1 处调用（已核实：line 279）；单元测试覆盖 |
| fundingInfo 未覆盖币种的间隔判断 | 官方明示缺省=8h；保留 history 推算兜底交叉验证 |
| Binance 语义标注后 auto 模式可执行机会减少 | 这是**预期行为**（误报被拦截），日志中保留被拦截机会的完整数据供复盘 |
| ccxt 升级引入其他行为变化 | 版本锁 `<5.0`；集成验证覆盖 5 所全字段比对 |

回滚：所有改动向后兼容（只增字段），`git revert` 即可，无数据迁移回滚负担。

---

## 8. 附录：关键官方文档引用存档

抓取日期均为 2026-10-19：

- Binance: `developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price` 与 `.../Get-Funding-Info`（经 jina 镜像）
- OKX: `okx.com/docs-v5/en/` "Get funding rate" 章节 + `GET /api/v5/public/funding-rate?instId=BTC-USDT-SWAP` 实测
- Bybit: `bybit-exchange.github.io/docs/v5/market/tickers` 与 `.../instrument`
- Gate: `github.com/gateio/gateapi-python/blob/master/docs/Contract.md` + `GET /futures/usdt/contracts/BTC_USDT` 实测
- Bitget: `bitget.com/docs/catalog/market/derivatives` "Get Current Funding Rate" 章节（注：旧 /api-doc/ 站点已下线）
