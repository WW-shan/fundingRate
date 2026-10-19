# 资金费率监控系统

加密货币资金费率与稳定币锚定的**纯监控系统**（无交易功能）：
多交易所公开数据采集 → 异常检测 → Telegram 实时推送 → Web 监控面板。

## 功能特性

### 监控能力

- **资金费率 APR 排行**：五家交易所全市场扫描，结算间隔归一化后按真实 APR 排序
- **跨所费率差**：两腿各自年化后求差，自动标注周期不一致 / 语义混用（Binance 为上期已结算值）
- **期现基差监控**：现货 vs 永续价格偏离，含陈旧行情过滤与合理性防护
- **稳定币脱锚监控**：USDC / FDUSD / TUSD / DAI / USDE 等，分级告警（0.3% 预警 / 1% 严重）

### 告警推送（Telegram）

| 告警类型 | 触发条件（可配置） | 默认阈值 | 是否推送 TG |
|---|---|---|---|
| 资金费率异常 | 年化 APR 超限 | 1000% | ✅ 仅推 APR ≥ 1000% |
| 稳定币脱锚 | 偏离 1.0 超限 | 0.3% / 1% | ✅ 始终推送 |
| 跨所费率差 | 年化差超限 | 100% | ❌ 仅落库（面板查看） |
| 基差异常 | 期现偏离超限 | 2% | ❌ 仅落库（面板查看） |

**推送消息内容**（资金费率）：
```
🔴 极端资金费率
币种: CT/USDT
交易所: GATE
当前费率: -2.0000%
收费频率: 每 4h（每日 6 次）
年化 APR: -4380.0%
费率类型: 下期将收取
下次结算: 2026-10-20 00:00:00
```

- 去重 + 冷却期（同标的冷却期内只推一次，级别升级立即重推）
- 每轮扫描推送上限（默认 10 条，防刷屏），超出部分仅落库供面板查询
- 每日汇总推送（可配置时间）
- 推送阈值可在 Web 配置页调整（`push_funding_apr_threshold`）

### Web 面板

1. **仪表盘** (`/`)：APR 排行 / 跨所差 / 稳定币状态 / 最近告警
2. **资金费率** (`/funding`)：APR 排行、跨所费率差、期现基差三个视图，支持过滤排序
3. **稳定币监控** (`/stablecoin`)：实时锚定状态 + 历史价格曲线
4. **告警历史** (`/alerts`)：按时间/级别/类型过滤，含统计卡片
5. **系统配置** (`/config`)：阈值、刷新间隔、交易所开关热更新

## 快速开始

### 1. 环境要求

- Python 3.10+
- 能访问交易所公开 API 的网络（受限地区需配置代理，见下）

### 2. 安装

```bash
git clone <your-repo-url>
cd fundingRate
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
```

关键配置项：

```bash
# Telegram 推送（可选，不配置则仅 Web 面板）
TG_BOT_TOKEN=your_bot_token
TG_CHAT_ID=your_chat_id

# Web 认证
WEB_USERNAME=admin
WEB_PASSWORD_HASH=          # 留空则默认密码 admin123

# 代理（受限网络环境按交易所配置）
BINANCE_PROXY=http://127.0.0.1:10808
OKX_PROXY=http://127.0.0.1:10808
# ... 或统一配置 PROXY_URL=http://127.0.0.1:10808
```

### 4. 运行

```bash
python main.py
```

访问 http://localhost:5000

## 项目结构

```
fundingRate/
├── exchanges/          # 交易所适配器（仅公开 API）
│   ├── base_exchange.py    # 基类：费率/行情/订单簿 + 官方间隔字段解析
│   └── *_adapter.py        # Binance/OKX/Bybit/Gate/Bitget
├── core/
│   ├── data_collector.py   # 批量采集（费率/价格/稳定币）
│   ├── market_monitor.py   # APR 计算 + 异常检测
│   └── alert_manager.py    # 告警去重/冷却/限流/落库
├── bot/                # Telegram 推送 + 查询命令
├── web/                # Flask 面板（5 页面 + REST API）
├── database/           # SQLite（funding_rates/market_prices/stablecoin_prices/alerts）
├── config/             # 三层配置体系（热更新）
├── utils/              # 日志工具
└── main.py             # 主入口
```

## 数据准确性设计

### 结算间隔归一化

各交易所结算周期不同（1h/2h/4h/8h），系统从官方字段读取逐币种间隔：

| 交易所 | 间隔来源 | 费率语义 |
|---|---|---|
| Binance | `fundingInfo.fundingIntervalHours`（批量） | 上期已结算 |
| OKX | `nextFundingTime − fundingTime`（官方推荐） | 下期预测 |
| Bybit | tickers `fundingIntervalHour` / instruments `fundingInterval` | 下期将收取 |
| Gate | `funding_interval`（秒） | 下期将收取 |
| Bitget | `fundingRateInterval`（小时） | 下期将收取 |

APR 计算：`单期费率 × (365×24h / 结算间隔) × 100%`（线性年化，与交易所官方展示口径一致）。

跨所比较时两腿各自年化后求差，并标注 `interval_mismatch`（周期不一致）与 `semantics_mixed`（含上期值）。

### 数据质量防护

- 陈旧行情过滤：ticker 时间戳超过阈值（默认 600s）丢弃，避免退市币种残留价格污染基差
- 基差合理性防护：|基差| > 50% 视为数据异常丢弃
- 批量接口优先：每家交易所单次调用获取全市场数据（Binance 间隔接口权重为 0）

## Telegram 命令

- `/apr` - APR 排行 Top10
- `/spread` - 跨所费率差 Top10
- `/basis` - 基差 Top10
- `/stable` - 稳定币锚定状态
- `/alerts` - 最近告警
- `/status` - 系统状态

## 部署

### Docker

```bash
docker-compose up -d
```

### systemd

```ini
[Unit]
Description=Funding Rate Monitor
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/fundingRate
ExecStart=/path/to/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## 免责声明

本系统仅提供市场数据监控与告警，不执行任何交易操作。数据仅供参考，不构成投资建议。
