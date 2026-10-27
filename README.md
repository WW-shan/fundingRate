# 资金费率监控系统

加密货币资金费率与锚定资产的**纯监控系统**（无交易功能）：多交易所公开数据采集 → 异常检测 → Telegram 推送 → Web 面板。

## 功能

- **资金费率 APR 排行**：Binance / OKX / Bybit / Gate / Bitget 全市场扫描，按每个币的真实结算周期线性年化。
- **跨所费率差 / 期现基差**：跨所年化差、现货与永续价差，含陈旧行情过滤。
- **锚定资产偏离**：稳定币、欧元稳定币、黄金/白银/原油等大宗商品；股票代币使用跨产品比价。
- **跨产品 / 跨发行商价差**：同一标的的 bStock / Ondo / xStocks / OKX / gStocks / Bitget Reality 版本比价。
- **Telegram 推送**：极端资金费率（默认 |APR| ≥ 1000%，且同一标的至少 2 家交易所同时异常）、期现基差、脱锚、跨产品/跨发行商价差；每轮最多 20 条，每条告警单独发送；同一标的的多所资金费率异常合并为一条。
- **跨交易所爆仓量监控**：聚合 Binance / OKX / Bybit / Gate / Bitget 公开强平数据，按币种和多空方向统计 USD 金额，识别 60 秒滚动窗口内的突然放大。
- **Web 面板**：仪表盘、资金费率、锚定资产、爆仓量、告警历史（不提供 Web 配置入口）。

## 数据来源与口径

| 交易所 | 资金费率来源 | 结算间隔来源 | 费率语义 |
|---|---|---|---|
| Binance | `GET /fapi/v1/premiumIndex` | `GET /fapi/v1/fundingInfo`（仅调整过的币种；缺省 8h） | 当前预测/下期将收取 |
| OKX | `GET /api/v5/public/funding-rate` | `nextFundingTime - fundingTime`（官方推荐） | 下期预测 |
| Bybit | `GET /v5/market/tickers` | tickers `fundingIntervalHour` / instruments `fundingInterval` | 下期将收取 |
| Gate | `GET /futures/usdt/contracts` | `funding_interval`（秒） | 下期将收取 |
| Bitget | `GET /api/v2/mix/market/current-fund-rate` | `fundingRateInterval`（小时） | 下期将收取 |

APR 公式：

```
APR = 单期费率 × (365 × 24 / 结算间隔小时)
```

各交易所、各币种结算周期不同（1h / 2h / 4h / 8h），系统逐币归一化，不做固定 8h 假设。

注意：

- Binance 的 `lastFundingRate` 字段名有误导性：它是当前预测/下期将收取费率，并非上一期已结算值；上一期结算值需查 `/fapi/v1/fundingRate`。五家统一按下期费率比较。
- XAUT/PAXG 以 XAU/USD 现货黄金为参考（gold-api 主源、Swissquote 备用、Yahoo GC=F 兜底）；SLVON/IAUON/GLDX 仍使用对应 ETF 价格。
- 股票代币不使用 Yahoo 价格判定；改为与其他发行商同标的产品中位价比较，无同类产品时仅展示价格、不计算偏离。
- 股票/商品代币映射偏离超过 50% 视为符号或份额比例异常，不参与脱锚判定；稳定币不受此过滤，极端脱锚仍会告警。

## 锚定资产范围

- 稳定币：USDC、FDUSD、PYUSD、RLUSD、USD1、USDE、USDS、USDG、USDD、XUSD、BUSD、DAI、BFUSD、RWUSD、USDGO、USDTB。
- 欧元稳定币：EUR、EURI。
- 大宗商品：XAUT、PAXG、SLVON、IAUON、GLDX（当前实际有报价的通常只有 XAUT/PAXG）。
- 股票代币：Binance bStock、Ondo、Backed xStocks、OKX Unified、Gate gStocks、Bitget Reality 的高流动性标的。
- 流动性过滤：按 24h 全市场成交额聚合，默认门槛 500,000 USD；低流动性、映射错误、已崩盘资产在 `config/peg_assets.py` 的 `EXCLUDED` 中排除。

## 告警策略

| 类型 | 默认阈值 | Telegram |
|---|---|---|
| 资金费率异常 | \|APR\| ≥ 1000% 且同一标的 ≥ 2 家交易所 | 推送 |
| 锚定资产脱锚 | USD/EUR 稳定币与大宗商品均为 5%/10%（预警/严重） | 推送（股票代币按下方跨发行商阈值） |
| 跨产品 / 跨发行商价差 | ≥ 5%（含股票代币） | 推送 |
| 跨所费率差 | ≥ 100% | 仅落库 |
| 期现基差 | 告警 ≥ 5%，推送 ≥ 5%（适用于所有现货/永续配对标的，包括股票代币） | 推送 |

**流动性过滤**

- 资金费率排行、跨所费率差、期现基差默认要求 **24h 永续成交额 ≥ 500,000 USD**（`global.funding_min_volume_usd`）。
- 低于门槛的标的仍会被采集和落库，但不进入排行、面板机会列表和 Telegram 告警。
- 跨所价差要求两条腿都达标；基差要求现货和永续都达标。
- 该门槛通过后端配置/数据库调整，Web 不提供配置入口。

**降噪规则（v2）**

- **汇总推送**：每轮扫描的所有待推送告警合并为一条 Telegram 消息，不再逐条发送。
- **分档推送**：同一 `(类型, 标的, 交易所)` 仅在首次出现、严重度跨档上升时推送；同档位持续存在时最长 24h 提醒一次（`push_reminder_cooldown`）。
- **恢复重置**：异常连续 1h 未出现视为恢复，再次出现会重新推送（`push_reset_gap`）。
- **基差阈值**：生成告警与 Telegram 推送默认均为 5%，适用于所有具备现货/永续配对的标的，包括股票代币；低于 5% 不生成基差告警（`basis_threshold`、`push_basis_threshold`）。跨发行商价差是独立告警类型，不使用该基差阈值。
- **单轮上限**：`max_pushes_per_scan` 默认 20，限制每轮发送的告警条数。

**股票代币比价（跨产品）**

- 股票代币**不再使用 Yahoo 股价判定脱锚**；Yahoo 价格会受盘前/盘后、延迟和份额比例影响，容易误报。
- 有多个发行商版本时，参考价为**其他发行商同标的产品中位价**，面板显示「跨产品」。
- 无同类产品可比时，面板标记「无同类可比」，偏离显示为 `—`，不计算偏离、不参与排序和告警。
- 跨产品价差告警由「跨产品 / 跨发行商价差」检测承担（默认阈值 5%，股票代币同样适用）。
- 欧元稳定币仍使用 Yahoo 参考价；XAUT/PAXG 使用现货黄金 API；ETF 类商品仍使用 Yahoo ETF 价格。其标的休市时同样改用同类中位价，无同类则仅展示。

## 爆仓量监控

五家交易所统一折算为 USD 名义金额，并按 `币种 + long/short` 汇总：

| 交易所 | 公开数据源 | 说明 |
|---|---|---|
| Binance | `!forceOrder@arr` WebSocket | 每个交易对每秒最多推送最新一笔强平；用 `markPrice@1s` 校验线路，默认直连优先，可用 `BINANCE_PROXY` 单独指定代理 |
| OKX | `liquidation-orders` WebSocket | 只覆盖近期强平，官方说明不代表全部强平量 |
| Bybit | `allLiquidation.{symbol}` WebSocket | 订阅全部 USDT 线性永续合约 |
| Gate | `/futures/usdt/liq_orders` REST | 每 2 秒轮询；持久连接、自动重试和失败退避；Gate 强平 WebSocket 需要账户鉴权 |
| Bitget | UTA `liquidation` WebSocket | 每秒按交易对推送多空最大强平记录 |

默认检测规则：

- 当前窗口：最近 60 秒。
- 常态基准：过去最多 30 个完整分钟的平均爆仓量，只统计启动后实际观察到的分钟。
- 当前金额至少 500,000 USD，且达到常态的 3 倍才推送。
- 达到常态 6 倍或当前窗口 2,000,000 USD 升级为严重告警。
- 启动后预热 5 分钟，并至少 2 家交易所在线才触发告警。
- 连接成功但 120 秒没有任何消息时会标为“无数据”，不计入在线数据源。
- 默认同时监控空单和多单；空单强平通常对应挤空，多单强平通常对应下跌加速。
- 落库冷却 5 分钟；同一档位最多每 10 分钟提醒一次。若 3 分钟未再次触发，会视为新一轮行情。

所有阈值都可在数据库 `config` 表的 `alerts.liquidation_*` 项中调整。Web 面板入口为 `/liquidations`，分钟聚合保留 8 天。

## 快速开始

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

访问 `http://localhost:5000`。默认用户名 `admin`，未配置 `WEB_PASSWORD_HASH` 时密码为 `admin123`（请在 `.env` 中修改）。

### 代理

受限网络环境可在 `.env` 中配置统一代理：

```bash
PROXY_URL=http://127.0.0.1:10808
```

也可按交易所单独配置 `BINANCE_PROXY` / `OKX_PROXY` / `BYBIT_PROXY` / `GATE_PROXY` / `BITGET_PROXY`。Telegram 默认复用 `PROXY_URL`，也可单独设置 `TG_PROXY`。

## Telegram

```bash
TG_BOT_TOKEN=...
TG_CHAT_ID=...
```

命令：`/apr`（极端 APR，正负均列）、`/spread`、`/basis`、`/stable`、`/liquidations`、`/alerts`、`/status`、`/help`。

## 项目结构

```
fundingRate/
├── exchanges/          # 5 家交易所适配器（公开 API）
├── core/
│   ├── data_collector.py   # 资金费率 / 价格 / 锚定资产 / 参考价采集
│   ├── market_monitor.py   # APR、跨所差、基差、脱锚、跨发行商检测
│   ├── liquidation_monitor.py # 五家交易所爆仓量聚合与异动检测
│   └── alert_manager.py    # 去重、冷却、限流、落库、推送
├── bot/                # Telegram 推送与查询
├── web/                # Flask 面板与 REST API
├── database/           # SQLite（最新快照 + 告警 + 有限历史）
├── config/             # 配置与锚定资产注册表
├── utils/              # 日志
└── main.py
```

## 数据保留

- `market_prices_latest`：每所每币仅保留最新一行，避免基差数据无限膨胀。
- `funding_rates`：1 天。
- `stablecoin_prices`：8 天（面板最长 7 天）。
- `liquidation_minutes`：8 天（按分钟、交易所、币种和多空方向聚合）。
- `reference_prices`：1 天。
- `alerts`：30 天。

## 测试

```bash
python -m unittest discover -s tests -v
python -X utf8 scripts/verify_all_exchanges.py
python -X utf8 scripts/verify_each_exchange.py   # 五家逐所对账，任一失败返回非零
```

逐所全量对账脚本会并发读取系统采集结果与官方原始接口，逐条核对资金费率、周期、结算时间、价格、锚定资产和内部公式；`--exchange okx` 可单独验证一所。

覆盖 OKX 2h 毫秒间隔、各所间隔单位、APR 归一化、Gate 官方市场 ID 归一化、推送优先级、USDT/黄金分类与份额比例等关键回归点。

## 部署

```bash
docker-compose up -d
```

健康检查：`GET /api/health`。

## 免责声明

本系统仅提供市场数据监控与告警，不执行任何交易操作。数据仅供参考，不构成投资建议。
