# 资金费率监控系统

加密货币资金费率与锚定资产的**纯监控系统**（无交易功能）：多交易所公开数据采集 → 异常检测 → Telegram 推送 → Web 面板。

## 功能

- **资金费率 APR 排行**：Binance / OKX / Bybit / Gate / Bitget 全市场扫描，按每个币的真实结算周期线性年化。
- **跨所费率差 / 期现基差**：跨所年化差、现货与永续价差，含陈旧行情过滤。
- **锚定资产脱锚**：稳定币、欧元稳定币、黄金/白银/原油等大宗商品、股票代币。
- **跨发行商价差**：同一标的的 bStock / Ondo / xStocks / OKX / gStocks / Bitget Reality 版本比价。
- **Telegram 推送**：极端资金费率（默认 |APR| ≥ 1000%）、脱锚、跨发行商价差；每轮最多 10 条。
- **Web 面板**：仪表盘、资金费率、锚定资产、告警历史、配置。

## 数据来源与口径

| 交易所 | 资金费率来源 | 结算间隔来源 | 费率语义 |
|---|---|---|---|
| Binance | `GET /fapi/v1/premiumIndex` | `GET /fapi/v1/fundingInfo`（仅调整过的币种；缺省 8h） | 上期已结算 |
| OKX | `GET /api/v5/public/funding-rate` | `nextFundingTime - fundingTime`（官方推荐） | 下期预测 |
| Bybit | `GET /v5/market/tickers` | tickers `fundingIntervalHour` / instruments `fundingInterval` | 下期将收取 |
| Gate | `GET /futures/usdt/contracts` | `funding_interval`（秒） | 下期将收取 |
| Bitget | `GET /api/v3/market/current-fund-rate` | `fundingRateInterval`（小时） | 下期将收取 |

APR 公式：

```
APR = 单期费率 × (365 × 24 / 结算间隔小时)
```

各交易所、各币种结算周期不同（1h / 2h / 4h / 8h），系统逐币归一化，不做固定 8h 假设。

注意：

- Binance 公开接口只提供**上期已结算**费率，面板会标注“上期”；其他四家为下一期预测/待收取费率。
- 黄金代币以 Yahoo `GC=F` 黄金期货为参考，存在期现基差，因此大宗商品阈值放宽为 2% / 5%。
- 股票代币在标的休市时参考价为最近收盘价，休市期间只推送 critical 脱锚。
- 映射偏离超过 50% 视为符号/份额比例异常，不参与脱锚判定。

## 锚定资产范围

- 稳定币：USDC、FDUSD、PYUSD、RLUSD、USD1、USDE、USDS、USDG、USDD、XUSD、BUSD、DAI、BFUSD、RWUSD、USDGO、USDTB。
- 欧元稳定币：EUR、EURI。
- 大宗商品：XAUT、PAXG、SLVON、IAUON、GLDX、RSGOL、RGOLD、RFOIL。
- 股票代币：Binance bStock、Ondo、Backed xStocks、OKX Unified、Gate gStocks、Bitget Reality 的高流动性标的。
- 流动性过滤：按 24h 全市场成交额聚合，默认门槛 500,000 USD；低流动性、映射错误、已崩盘资产在 `config/peg_assets.py` 的 `EXCLUDED` 中排除。

## 告警策略

| 类型 | 默认阈值 | Telegram |
|---|---|---|
| 资金费率异常 | \|APR\| ≥ 1000% | 推送 |
| 锚定资产脱锚 | 稳定币 0.3%/1%，大宗 2%/5%，股票 2%/5% | 推送（休市 warning 除外） |
| 跨发行商价差 | ≥ 1% | 推送 |
| 跨所费率差 | ≥ 100% | 仅落库 |
| 期现基差 | ≥ 2% | 仅落库 |

- 同一 `(类型, 标的, 交易所)` 有落库冷却与推送冷却；级别升级立即重推。
- 每轮最多推送 10 条，防止极端行情刷屏。

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

也可按交易所单独配置 `BINANCE_PROXY` / `OKX_PROXY` / `BYBIT_PROXY` / `GATE_PROXY` / `BITGET_PROXY`。

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
```

覆盖 OKX 2h 毫秒间隔、各所间隔单位、APR 归一化、USDT/黄金分类与份额比例等关键回归点。

## 部署

```bash
docker-compose up -d
```

健康检查：`GET /api/health`。

## 免责声明

本系统仅提供市场数据监控与告警，不执行任何交易操作。数据仅供参考，不构成投资建议。
