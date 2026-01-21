# 资金费率套利系统 - 设计文档

**日期**: 2026-01-21
**版本**: 1.0.0
**作者**: Claude & User

## 1. 项目概述

资金费率套利系统是一个自动化的加密货币套利程序，支持三种套利策略：
1. 跨交易所资金费率套利
2. 现货-期货资金费率套利（自动执行）
3. 现货-期货基差套利（半自动，需确认）

### 核心特性

- **KISS原则**: 简单清晰的单进程架构
- **全配置化**: 所有参数可在Web界面实时调整
- **智能监控**: 实时扫描全市场，自动计算最优策略
- **成本精算**: 手续费、滑点全部计入收益计算
- **分级执行**: 低风险策略自动执行，高风险策略需人工确认
- **实时通知**: Telegram Bot推送和远程控制
- **完整回测**: 参数优化、性能分析、可视化

### 技术栈

- **语言**: Python 3.10+
- **Web框架**: Flask + Bootstrap 5 + jQuery
- **交易所API**: ccxt
- **Bot**: python-telegram-bot
- **数据库**: SQLite
- **数据分析**: pandas + numpy
- **可视化**: matplotlib + seaborn

## 2. 系统架构

### 2.1 整体架构

单进程多线程架构，包含以下核心模块：

```
┌─────────────────────────────────────────────────┐
│              Main Process                        │
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌──────────────┐    ┌──────────────┐          │
│  │ ConfigManager│◄───┤ DatabaseMgr  │          │
│  └──────┬───────┘    └──────────────┘          │
│         │                                        │
│  ┌──────▼───────────────────────────────────┐  │
│  │       DataCollector (Thread)             │  │
│  │  - 实时采集价格、资金费率               │  │
│  │  - 历史数据导入                          │  │
│  └──────┬───────────────────────────────────┘  │
│         │                                        │
│  ┌──────▼───────────────────────────────────┐  │
│  │   OpportunityMonitor (Thread)            │  │
│  │  - 扫描套利机会                          │  │
│  │  - 计算最优策略                          │  │
│  │  - 评分排序                              │  │
│  └──────┬───────────────────────────────────┘  │
│         │                                        │
│  ┌──────▼───────────────────────────────────┐  │
│  │   StrategyExecutor (Thread)              │  │
│  │  - 执行套利策略                          │  │
│  │  - 持仓管理                              │  │
│  └──────┬───────────────────────────────────┘  │
│         │                                        │
│  ┌──────▼───────────────────────────────────┐  │
│  │       RiskManager                        │  │
│  │  - 风控检查                              │  │
│  │  - 多级预警                              │  │
│  └──────────────────────────────────────────┘  │
│                                                  │
│  ┌──────────────────────────────────────────┐  │
│  │    Flask Web App (Main Thread)           │  │
│  │  - REST API                              │  │
│  │  - 前端界面                              │  │
│  └──────────────────────────────────────────┘  │
│                                                  │
│  ┌──────────────────────────────────────────┐  │
│  │    Telegram Bot (Thread)                 │  │
│  │  - 推送通知                              │  │
│  │  - 远程控制                              │  │
│  └──────────────────────────────────────────┘  │
│                                                  │
└─────────────────────────────────────────────────┘
```

### 2.2 模块职责

**ConfigManager (配置管理器)**
- 从SQLite加载配置到内存
- 支持配置热更新（三层配置：全局/策略/交易对）
- 提供配置变更事件通知

**DataCollector (数据采集器)**
- 使用ccxt统一封装5个交易所API (Binance, OKX, Bybit, Gate, Bitget)
- 实时采集：价格、资金费率、账户余额、持仓
- 历史数据导入：支持CSV格式
- 定时任务：每5秒价格，每5分钟资金费率

**OpportunityMonitor (机会监控系统)**
- 每10秒扫描全市场
- 计算三种策略的所有可能组合
- 精确计算：开仓成本、平仓成本、手续费、滑点
- 评分排序：综合收益、风险、流动性
- 推送到Web和TG Bot

**StrategyExecutor (策略执行引擎)**
- 接收机会并根据风险等级决定执行方式
- 低风险自动执行，高风险等待确认
- 管理订单生命周期
- 持仓监控和自动平仓

**RiskManager (风险管理器)**
- 基础风控：单笔仓位、总资金使用率、止损线
- 进阶风控：动态仓位调整、多级预警、异常检测
- 实时监控所有持仓

**WebUI (Web界面)**
- 7个主要页面：仪表盘、机会监控、持仓管理、策略配置、回测系统、数据管理、系统设置
- 实时更新（WebSocket或轮询）
- 响应式设计（Bootstrap 5）

**TGBot (Telegram机器人)**
- 推送：开仓/平仓通知、风险预警、每日报告
- 查询：持仓、余额、策略状态
- 控制：暂停/恢复策略、紧急平仓

**BacktestEngine (回测引擎)**
- 基于历史数据模拟交易
- 参数优化（网格搜索）
- 性能分析：收益率、夏普比率、最大回撤
- 可视化图表

## 3. 数据库设计

### 3.1 表结构

**config (配置表)**
```sql
CREATE TABLE config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category VARCHAR(50),
    key VARCHAR(100),
    value TEXT,
    is_hot_reload BOOLEAN DEFAULT TRUE,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**exchange_accounts (交易所账户表)**
```sql
CREATE TABLE exchange_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_name VARCHAR(20) UNIQUE,
    api_key TEXT,
    api_secret TEXT,
    passphrase TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**trading_pair_configs (交易对配置表)**
```sql
CREATE TABLE trading_pair_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol VARCHAR(20),
    exchange VARCHAR(20),

    strategy1_enabled BOOLEAN DEFAULT TRUE,
    strategy2a_enabled BOOLEAN DEFAULT TRUE,
    strategy2b_enabled BOOLEAN DEFAULT TRUE,

    s1_execution_mode VARCHAR(10) DEFAULT 'auto',
    s1_min_funding_diff DECIMAL(10,6),
    s1_position_size DECIMAL(18,2),
    s1_target_exchanges TEXT,

    s2a_execution_mode VARCHAR(10) DEFAULT 'auto',
    s2a_min_funding_rate DECIMAL(10,6),
    s2a_position_size DECIMAL(18,2),
    s2a_max_basis_deviation DECIMAL(10,6),

    s2b_execution_mode VARCHAR(10) DEFAULT 'manual',
    s2b_min_basis DECIMAL(10,6),
    s2b_position_size DECIMAL(18,2),
    s2b_target_return DECIMAL(10,6),

    max_positions INTEGER DEFAULT 3,
    priority INTEGER DEFAULT 5,
    is_active BOOLEAN DEFAULT TRUE,
    notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**klines (历史K线表)**
```sql
CREATE TABLE klines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange VARCHAR(20),
    symbol VARCHAR(20),
    timeframe VARCHAR(10),
    timestamp BIGINT,
    open DECIMAL(18,8),
    high DECIMAL(18,8),
    low DECIMAL(18,8),
    close DECIMAL(18,8),
    volume DECIMAL(18,8),
    UNIQUE(exchange, symbol, timeframe, timestamp)
);
CREATE INDEX idx_klines ON klines(exchange, symbol, timestamp);
```

**funding_rates (资金费率历史表)**
```sql
CREATE TABLE funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange VARCHAR(20),
    symbol VARCHAR(20),
    timestamp BIGINT,
    funding_rate DECIMAL(10,6),
    next_funding_time BIGINT,
    UNIQUE(exchange, symbol, timestamp)
);
CREATE INDEX idx_funding_rates ON funding_rates(exchange, symbol, timestamp);
```

**orders (订单记录表)**
```sql
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER,
    strategy_type VARCHAR(50),
    exchange VARCHAR(20),
    symbol VARCHAR(20),
    side VARCHAR(10),
    order_type VARCHAR(10),
    price DECIMAL(18,8),
    amount DECIMAL(18,8),
    filled DECIMAL(18,8),
    status VARCHAR(20),
    order_id VARCHAR(100),
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    update_time TIMESTAMP
);
CREATE INDEX idx_orders ON orders(strategy_type, create_time);
```

**positions (持仓表)**
```sql
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_type VARCHAR(50),
    symbol VARCHAR(20),
    exchanges TEXT,
    entry_details TEXT,
    position_size DECIMAL(18,2),
    current_pnl DECIMAL(18,2),
    realized_pnl DECIMAL(18,2),
    funding_collected DECIMAL(18,2),
    fees_paid DECIMAL(18,2),
    status VARCHAR(20),
    open_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    close_time TIMESTAMP
);
CREATE INDEX idx_positions ON positions(status, open_time);
```

**strategy_logs (策略执行记录表)**
```sql
CREATE TABLE strategy_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_type VARCHAR(50),
    action VARCHAR(50),
    details TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**risk_events (风险事件表)**
```sql
CREATE TABLE risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level VARCHAR(20),
    event_type VARCHAR(50),
    description TEXT,
    position_id INTEGER,
    is_handled BOOLEAN DEFAULT FALSE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**backtest_results (回测结果表)**
```sql
CREATE TABLE backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100),
    strategy_type VARCHAR(50),
    strategy_params TEXT,
    start_date DATE,
    end_date DATE,
    initial_capital DECIMAL(18,2),
    final_capital DECIMAL(18,2),
    total_return DECIMAL(10,4),
    annual_return DECIMAL(10,4),
    sharpe_ratio DECIMAL(10,4),
    max_drawdown DECIMAL(10,4),
    win_rate DECIMAL(10,4),
    total_trades INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 4. 三种套利策略

### 4.1 策略1：跨交易所资金费率套利

**核心逻辑**：
- 监控同一币种在不同交易所的资金费率差异
- 在费率高的交易所做空（收取高资金费率）
- 在费率低的交易所做多（支付低资金费率）
- 净收益 = 费率差 - 手续费

**开仓条件**：
- 费率差 > 最小费率差阈值（可配置，如0.05%）
- 净收益率 > 最小净收益率（扣除手续费后，如0.03%）
- 两个交易所价格差异 < 2%（避免价格异常）
- 通过风控检查
- 流动性充足（订单簿深度 > 交易量10倍）

**平仓条件**：
- 费率差收窄到退出阈值
- 已收取N期资金费率（可配置）
- 触发止损
- 手动平仓

**收益计算**：
```python
# 单期收益（8小时）
funding_income = position_size * (short_exchange_rate - long_exchange_rate)

# 开仓成本
long_open_fee = position_size * long_exchange_taker_fee
short_open_fee = position_size * short_exchange_taker_fee
long_slippage = estimate_slippage(long_depth, trade_amount)
short_slippage = estimate_slippage(short_depth, trade_amount)

# 平仓成本（预估）
long_close_fee = position_size * long_exchange_maker_fee
short_close_fee = position_size * short_exchange_maker_fee

# 净收益
total_cost = long_open_fee + short_open_fee + long_close_fee + short_close_fee + long_slippage + short_slippage
net_profit = funding_income - total_cost
net_profit_pct = net_profit / position_size
```

**执行模式**: 自动（可配置为半自动）

### 4.2 策略2A：现货-期货资金费率套利

**核心逻辑**：
- 在同一交易所买入现货，开等量永续合约空单
- 主要目的是收取高额资金费率
- 基差必须在安全范围内，避免价格风险

**开仓条件**：
- 年化资金费率 ≥ 最小阈值（可配置，如30%）
- 基差在安全范围内（-1% 到 +1%，可配置）
- 单期净收益 > 0
- 通过风控检查

**平仓条件**：
- 资金费率降低到退出阈值
- 已收取N期资金费率
- 基差扩大超出安全范围
- 触发止损

**收益计算**：
```python
# 单期收益（8小时）
funding_income = position_size * funding_rate

# 开仓成本
spot_open_fee = position_size * spot_taker_fee
futures_open_fee = position_size * futures_taker_fee

# 平仓成本
spot_close_fee = position_size * spot_maker_fee
futures_close_fee = position_size * futures_maker_fee

# 基差风险
basis = (futures_price - spot_price) / spot_price
basis_risk = abs(basis) * position_size

# 净收益
total_cost = spot_open_fee + futures_open_fee + spot_close_fee + futures_close_fee
net_profit = funding_income - total_cost
net_profit_pct = net_profit / position_size
```

**执行模式**: 自动（推荐）

### 4.3 策略2B：现货-期货基差套利

**核心逻辑**：
- 当基差异常大时开仓，等待基差回归赚取价差
- 买入现货 + 开空单对冲
- 风险较高，需要人工判断

**开仓条件**：
- 基差 ≥ 最小基差阈值（可配置，如2%）
- 预期收益 > 目标收益率（扣除手续费和预估资金费率成本后）
- 基差偏离历史均值达到一定标准差
- TG Bot推送详情，等待人工确认

**平仓条件**：
- 基差回归到正常范围（如 < 0.5%）
- 达到目标收益率
- 达到最大持仓天数
- 触发止损

**收益计算**：
```python
# 基差收敛收益
basis_income = position_size * basis

# 开仓成本
spot_open_fee = position_size * spot_taker_fee
futures_open_fee = position_size * futures_taker_fee

# 平仓成本
spot_close_fee = position_size * spot_maker_fee
futures_close_fee = position_size * futures_maker_fee

# 持仓期间资金费率成本（预估持仓3天）
estimated_funding_cost = position_size * funding_rate * 9  # 3天 × 3次

# 净收益
total_cost = spot_open_fee + futures_open_fee + spot_close_fee + futures_close_fee + estimated_funding_cost
net_profit = basis_income - total_cost
net_profit_pct = net_profit / position_size
```

**执行模式**: 半自动（固定，需人工确认）

## 5. 监控系统

### 5.1 数据采集

**实时数据结构**：
```python
market_data = {
    "BTC/USDT": {
        "binance": {
            "spot_price": 43250.50,
            "spot_bid": 43250.00,
            "spot_ask": 43251.00,
            "futures_price": 43280.00,
            "futures_bid": 43279.50,
            "futures_ask": 43280.50,
            "funding_rate": 0.0001,
            "predicted_funding_rate": 0.00015,
            "next_funding_time": "2026-01-21 16:00:00",
            "spot_depth_5": 150.5,  # 前5档深度（BTC）
            "futures_depth_5": 200.3,
            "maker_fee": 0.0001,
            "taker_fee": 0.0004,
            "timestamp": 1737456789
        },
        "okx": {...},
        ...
    },
    ...
}
```

### 5.2 机会计算引擎

每个监控周期（10秒）：
1. 遍历所有交易对
2. 对每个交易对计算三种策略的所有可能组合
3. 精确计算开仓成本、平仓成本、手续费、滑点
4. 计算净收益和收益率
5. 评分排序
6. 推送到Web和TG Bot

**评分算法**：
```python
def calculate_score(net_profit_pct, risk_factor, bonus_factor):
    """
    综合评分 0-100
    net_profit_pct: 净收益率（越高越好）
    risk_factor: 风险因子（价差/基差，越低越好）
    bonus_factor: 加分项（如年化费率）
    """
    profit_score = min(net_profit_pct * 10000, 50)  # 最高50分
    risk_score = max(0, 30 - risk_factor * 1000)  # 最高30分
    bonus_score = min(bonus_factor / 10, 20)  # 最高20分

    return profit_score + risk_score + bonus_score
```

### 5.3 机会数据结构

```python
opportunity = {
    "id": "uuid",
    "type": "funding_rate_cross_exchange | funding_rate_spot_futures | basis_arbitrage",
    "risk_level": "low | medium | high",
    "score": 85.5,
    "symbol": "BTC/USDT",
    "exchanges": ["binance", "okx"],  # 或单个交易所
    "expected_return": 0.0045,
    "expected_return_usdt": 45.0,
    "position_size": 10000,
    "details": {
        # 具体数据，根据策略类型不同
        "funding_diff": 0.0025,  # 策略1
        "annual_funding_rate": 273,  # 策略2A
        "basis": 0.025,  # 策略2B
        "total_cost": 25,
        "net_profit": 45,
        ...
    },
    "detected_at": "2026-01-21 10:30:00",
    "status": "pending | executing | expired | ignored"
}
```

## 6. 风险管理

### 6.1 基础风控

- 单笔最大亏损：2%（可配置）
- 总资金最大回撤：10%（可配置）
- 单交易所最大仓位：30000 USDT（可配置）
- 总资金使用率：80%（可配置）
- 单策略最大占用：50%（可配置）

### 6.2 进阶风控

**动态仓位调整**：
```python
# 根据机会质量调整仓位
if score > 85:
    position_size = base_position_size * 1.5
elif score > 60:
    position_size = base_position_size * 1.0
else:
    position_size = base_position_size * 0.5
```

**多级预警**：
- 警告级别（黄色）：浮亏 > 0.5%
- 严重级别（橙色）：浮亏 > 1.0%
- 紧急级别（红色）：浮亏 > 1.5%

**异常检测**：
- 价格偏离：与其他交易所价差 > 2%
- 资金费率异常：单期费率 > 0.5%
- 订单簿深度不足：< 交易量的10倍

## 7. Web界面

### 7.1 页面列表

1. **仪表盘** - 总览、持仓摘要、热门机会
2. **机会监控** - 四种排行榜（收益、费率、基差、综合）
3. **持仓管理** - 当前持仓、详情、平仓
4. **策略配置** - 全局/策略/交易对三层配置
5. **回测系统** - 回测执行、参数优化、结果分析
6. **数据管理** - 历史数据导入、实时采集状态、导出
7. **系统设置** - 交易所配置、通知设置、日志

### 7.2 关键功能

**智能资金分配**：
- 智能分配：根据评分和收益自动分配
- 平均分配：每个机会相同资金
- 优先高收益：优先分配给收益率最高的
- 手动选择：手动勾选要执行的机会

**实时更新**：
- 机会监控页面每5秒刷新
- 持仓页面每10秒刷新
- WebSocket或AJAX轮询

**配置热更新**：
- 标注⚡的配置立即生效
- 其他配置需要重启（明确提示）

## 8. Telegram Bot

### 8.1 推送消息类型

1. **自动开仓通知** - 包含详情和操作按钮
2. **机会通知（需确认）** - 基差套利机会
3. **风险预警** - 三级预警推送
4. **每日报告** - 资金、持仓、策略表现、机会统计

### 8.2 命令列表

**查询命令**：
- `/balance` - 查看余额
- `/positions` - 查看持仓
- `/opportunities` - 查看当前机会
- `/status` - 系统状态
- `/report` - 今日报告

**控制命令**：
- `/pause` - 暂停所有策略
- `/resume` - 恢复所有策略
- `/close <ID>` - 平仓指定持仓
- `/closeall` - 紧急平仓所有持仓

## 9. 回测系统

### 9.1 功能特性

- 基于历史数据模拟交易
- 手续费、滑点精确模拟
- 参数网格搜索优化
- 性能指标：收益率、夏普比率、最大回撤、胜率、卡玛比率
- 可视化图表：权益曲线、收益分布、参数热图

### 9.2 参数优化

网格搜索所有参数组合，找到最优参数：
- 最小基差/费率差
- 目标收益率
- 最大持仓天数
- 止损比例

## 10. 部署

### 10.1 开发环境

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env
python main.py
```

### 10.2 生产环境

使用systemd管理服务：
```bash
sudo systemctl enable funding-arbitrage
sudo systemctl start funding-arbitrage
```

### 10.3 Docker部署

```bash
docker-compose up -d
```

## 11. 安全考虑

1. **API密钥加密存储** - 使用cryptography加密
2. **Web界面认证** - Flask session + 密码保护
3. **交易权限控制** - API只需要读取和交易权限，不需要提现
4. **日志审计** - 所有操作记录到数据库和日志文件
5. **备份策略** - 每日自动备份数据库

## 12. 监控与告警

1. **系统健康监控** - 进程状态、API连接状态
2. **性能监控** - API调用次数、延迟
3. **异常告警** - 连接失败、订单失败、风险事件
4. **每日报告** - TG Bot推送每日总结

## 13. 未来扩展

1. **更多策略** - 三角套利、跨链套利
2. **更多交易所** - 支持更多交易所
3. **机器学习** - 预测资金费率走势
4. **自动参数调优** - 根据市场变化自动调整参数
5. **多账户支持** - 支持多个账户并行运行

---

**设计完成日期**: 2026-01-21
**预计实施周期**: 2-3周
**优先级**: P0 (最高)
