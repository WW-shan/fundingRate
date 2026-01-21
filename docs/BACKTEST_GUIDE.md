# 回测系统使用指南

## 概述

回测系统允许使用历史资金费率数据验证套利策略的有效性。

## 功能特性

- 支持多策略回测
- 自动计算PnL、胜率、最大回撤等指标
- 生成详细的回测报告
- 可视化结果分析(权益曲线、盈亏分布、策略对比)
- 支持参数优化

## 使用方法

### 1. 命令行回测

使用 `scripts/run_backtest.py` 脚本:

```bash
# 基本用法
python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31

# 指定初始资金
python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31 --capital 50000

# 选择特定策略
python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31 --strategies strategy1

# 保存结果并生成报告
python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31 --save --report --charts
```

**参数说明:**

- `--start`: 回测开始日期 (YYYY-MM-DD) [必需]
- `--end`: 回测结束日期 (YYYY-MM-DD) [必需]
- `--capital`: 初始资金，默认100000 USDT
- `--strategies`: 策略列表，可选: strategy1, strategy2a, strategy2b
- `--name`: 回测名称(用于保存时标识)
- `--save`: 保存结果到数据库
- `--report`: 生成文本报告
- `--charts`: 生成图表

### 2. API回测

#### 运行回测

```bash
POST /api/backtest/run
Content-Type: application/json

{
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 100000,
  "strategies": ["strategy1", "strategy2a"],
  "name": "test_backtest_1"
}
```

**响应:**

```json
{
  "success": true,
  "data": {
    "total_trades": 150,
    "profitable_trades": 95,
    "losing_trades": 55,
    "total_pnl": 15230.50,
    "total_fees": 850.30,
    "win_rate": 63.33,
    "roi": 15.23,
    "initial_capital": 100000,
    "final_capital": 115230.50,
    "max_drawdown": 3.45,
    "trades": [...]
  }
}
```

#### 获取可用数据范围

```bash
GET /api/backtest/data_range
```

**响应:**

```json
{
  "success": true,
  "data": {
    "start_date": "2024-01-01 00:00:00",
    "end_date": "2024-12-31 23:59:59"
  }
}
```

#### 获取历史回测结果

```bash
GET /api/backtest/results
```

**响应:**

```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "name": "test_backtest_1",
      "timestamp": "2024-01-15 10:30:00",
      "initial_capital": 100000,
      "final_capital": 115230.50,
      "total_trades": 150,
      "win_rate": 63.33,
      "roi": 15.23,
      "max_drawdown": 3.45
    }
  ]
}
```

### 3. Python代码集成

```python
from database.db_manager import DatabaseManager
from config.config_manager import ConfigManager
from backtesting import BacktestEngine, ResultsAnalyzer

# 初始化
db_manager = DatabaseManager()
config_manager = ConfigManager()

# 创建回测引擎
engine = BacktestEngine(db_manager, config_manager)

# 运行回测
results = engine.run_backtest(
    start_date='2024-01-01',
    end_date='2024-12-31',
    initial_capital=100000,
    strategies=['strategy1', 'strategy2a']
)

# 分析结果
analyzer = ResultsAnalyzer()
report = analyzer.generate_report(results)
print(report)

# 生成图表
analyzer.generate_all_charts(results)
```

## 回测策略

### Strategy 1: 跨交易所套利

在不同交易所之间寻找资金费率差异，做多低费率交易所，做空高费率交易所。

**参数:**
- `min_spread`: 最小费率差 (默认: 0.0003 = 0.03%)

### Strategy 2a: 现货-合约套利

当资金费率足够高时，持有现货并做空合约收取资金费。

**参数:**
- `min_funding_rate`: 最小资金费率 (默认: 0.0005 = 0.05%)

### Strategy 2b: 双向合约套利

同时在两个交易所开多空仓位。

## 回测结果指标

- **Total Trades**: 总交易次数
- **Profitable Trades**: 盈利交易数
- **Losing Trades**: 亏损交易数
- **Total PnL**: 总盈亏 (USDT)
- **Total Fees**: 总手续费 (USDT)
- **Win Rate**: 胜率 (%)
- **ROI**: 投资回报率 (%)
- **Max Drawdown**: 最大回撤 (%)

## 图表说明

### 1. 权益曲线 (Equity Curve)

显示账户权益随时间的变化，直观展示策略的盈利能力和稳定性。

### 2. 盈亏分布 (PnL Distribution)

每笔交易盈亏的直方图，展示交易结果的分布特征。

### 3. 策略对比 (Strategy Comparison)

比较不同策略的总盈亏和胜率。

## 注意事项

1. **数据要求**: 需要足够的历史资金费率数据，建议至少3个月以上
2. **手续费**: 默认使用0.05%的手续费，可在代码中调整
3. **滑点**: 当前版本未考虑滑点影响
4. **资金限制**: 每个仓位最多使用10%的总资金
5. **持仓时间**: 默认最长持仓7天
6. **止盈止损**: 目标盈利1%，止损-0.5%

## 参数优化

可以通过修改策略参数来优化回测结果:

```python
# 自定义参数
parameters = {
    'min_spread': 0.0005,  # 提高最小费率差要求
    'min_funding_rate': 0.001,  # 提高最小资金费率要求
}

results = engine.run_backtest(
    start_date='2024-01-01',
    end_date='2024-12-31',
    initial_capital=100000,
    strategies=['strategy1'],
    parameters=parameters
)
```

## 故障排除

### 无可用数据

如果提示"No funding rate data found":

1. 检查数据库中是否有历史数据
2. 确认日期范围是否正确
3. 运行数据采集器收集历史数据

### 回测运行缓慢

对于大量数据的回测:

1. 缩小日期范围
2. 减少策略数量
3. 考虑使用更快的数据库

### 结果不合理

1. 检查策略参数是否合理
2. 验证历史数据质量
3. 确认手续费设置正确

## 后续改进

- [ ] 添加更多策略
- [ ] 支持参数网格搜索
- [ ] 实现遗传算法优化
- [ ] 增加Monte Carlo模拟
- [ ] 支持多币种同时回测
- [ ] 添加更多性能指标(Sharpe Ratio, Sortino Ratio等)
