# 资金费率监控系统

加密货币资金费率与锚定资产的**纯监控系统**（无交易功能）：多交易所公开数据采集 → 异常检测 → Telegram 推送 → Web 面板。

## 功能

- **资金费率 APR 排行**：Binance / OKX / Bybit / Gate / Bitget 全市场扫描，按每个币的真实结算周期线性年化。
- **跨所费率差 / 期现基差**：跨所年化差、现货与永续价差，含陈旧行情过滤。
- **锚定资产偏离**：稳定币、欧元稳定币、黄金/白银/原油等大宗商品；股票代币使用跨产品比价。
- **跨产品 / 跨发行商价差**：同一标的的 bStock / Ondo / xStocks / OKX / gStocks / Bitget Reality 版本比价。
- **Telegram 推送**：极端资金费率（默认 |APR| ≥ 1000%，且同一标的至少 2 家交易所同时异常）、期现基差、脱锚、跨产品/跨发行商价差；每轮最多 20 条并合并为一条汇总消息。跨所费率差只进面板。
- **Web 面板**：仪表盘、资金费率、锚定资产、告警历史、配置。

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
- 黄金代币以 Yahoo `GC=F` 黄金期货为参考，存在期现基差，因此大宗商品阈值放宽为 2% / 5%。
- 股票代币不使用 Yahoo 价格判定；改为与其他发行商同标的产品中位价比较，无同类产品时仅展示价格、不计算偏离。
- 股票/商品代币映射偏离超过 50% 视为符号或份额比例异常，不参与脱锚判定；稳定币不受此过滤，极端脱锚仍会告警。

## 锚定资产范围

- 稳定币：USDC、FDUSD、PYUSD、RLUSD、USD1、USDE、USDS、USDG、USDD、XUSD、BUSD、DAI、BFUSD、RWUSD、USDGO、USDTB。
- 欧元稳定币：EUR、EURI。
- 大宗商品：XAUT、PAXG、SLVON、IAUON、GLDX、RSGOL、RGOLD、RFOIL。
- 股票代币：Binance bStock、Ondo、Backed xStocks、OKX Unified、Gate gStocks、Bitget Reality 的高流动性标的。
- 流动性过滤：按 24h 全市场成交额聚合，默认门槛 500,000 USD；低流动性、映射错误、已崩盘资产在 `config/peg_assets.py` 的 `EXCLUDED` 中排除。

## 告警策略

| 类型 | 默认阈值 | Telegram |
|---|---|---|
| 资金费率异常 | \|APR\| ≥ 1000% 且同一标的 ≥ 2 家交易所 | 推送 |
| 锚定资产脱锚 | 稳定币 0.3%/1%，大宗 2%/5% | 推送（股票代币改为跨产品比价，见下） |
| 跨产品 / 跨发行商价差 | ≥ 1% | 推送 |
| 跨所费率差 | ≥ 100% | 仅落库 |
| 期现基差 | 落库 ≥ 2%，推送 ≥ 3% | 推送 |

**流动性过滤**

- 资金费率排行、跨所费率差、期现基差默认要求 **24h 永续成交额 ≥ 500,000 USD**（`global.funding_min_volume_usd`）。
- 低于门槛的标的仍会被采集和落库，但不进入排行、面板机会列表和 Telegram 告警。
- 跨所价差要求两条腿都达标；基差要求现货和永续都达标。
- 该门槛可在 Web「系统配置」页调整。

**降噪规则（v2）**

- **汇总推送**：每轮扫描的所有待推送告警合并为一条 Telegram 消息，不再逐条发送。
- **分档推送**：同一 `(类型, 标的, 交易所)` 仅在首次出现、严重度跨档上升时推送；同档位持续存在时最长 24h 提醒一次（`push_reminder_cooldown`）。
- **恢复重置**：异常连续 1h 未出现视为恢复，再次出现会重新推送（`push_reset_gap`）。
- **基差推送阈值**：默认 3%，2-3% 的轻微基差只落库/上面板（`push_basis_threshold`）。
- **单轮上限**：`max_pushes_per_scan` 默认 20，限制汇总消息内的条目数。

**股票代币比价（跨产品）**

- 股票代币**不再使用 Yahoo 股价判定脱锚**；Yahoo 价格会受盘前/盘后、延迟和份额比例影响，容易误报。
- 有多个发行商版本时，参考价为**其他发行商同标的产品中位价**，面板显示「跨产品」。
- 无同类产品可比时，面板标记「无同类可比」，偏离显示为 `—`，不计算偏离、不参与排序和告警。
- 跨产品价差告警由「跨产品 / 跨发行商价差」检测承担（默认阈值 1%）。
- 大宗商品/欧元稳定币仍使用 Yahoo 参考价；其标的休市时同样改用同类中位价，无同类则仅展示。

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

命令：`/apr`（极端 APR，正负均列）、`/spread`、`/basis`、`/stable`、`/alerts`、`/status`、`/help`。

## 项目结构

```
fundingRate/
├── exchanges/          # 5 家交易所适配器（公开 API）
├── core/
│   ├── data_collector.py   # 资金费率 / 价格 / 锚定资产 / 参考价采集
│   ├── market_monitor.py   # APR、跨所差、基差、脱锚、跨发行商检测
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
