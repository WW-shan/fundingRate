# 资金费率套利系统

一个专业的加密货币资金费率套利自动化交易系统，支持多交易所、多策略、Web管理界面和Telegram Bot通知。

## 功能特性

### 三种套利策略

1. **跨交易所资金费率套利**
   - 在不同交易所之间利用资金费率差异套利
   - 自动扫描所有交易所组合
   - 精确计算手续费和滑点成本
   - 支持自动/半自动执行

2. **现货-期货资金费率套利**
   - 持有现货+永续合约空单，收取高额资金费率
   - 基差风险控制
   - 自动执行（推荐）

3. **现货-期货基差套利**
   - 当基差异常时开仓，等待基差回归
   - 需人工确认（风险较高）
   - TG Bot推送详细分析

### 核心功能

- ✅ **智能监控**: 实时扫描全市场，自动计算最优策略组合
- ✅ **成本精算**: 手续费、滑点、资金费率全部计入
- ✅ **风险管理**: 基础+进阶风控，多级预警，异常检测
- ✅ **Web界面**: 7个管理页面，实时配置，热更新
- ✅ **TG Bot**: 推送通知、远程查询、紧急控制
- ✅ **回测系统**: 参数优化、性能分析、可视化
- ✅ **数据管理**: 历史数据导入、实时采集、自动备份

### 支持的交易所

- Binance (币安)
- OKX
- Bybit
- Gate.io
- Bitget

## 快速开始

### 1. 环境要求

- Python 3.10+
- pip
- SQLite (Python内置)

### 2. 安装

```bash
# 克隆项目
git clone <your-repo-url>
cd fundingRate

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\\Scripts\\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入你的API密钥
nano .env
```

**重要配置项**:
```bash
# 交易所API（至少配置一个）
BINANCE_API_KEY=your_api_key
BINANCE_API_SECRET=your_api_secret

# Telegram Bot（可选）
TG_BOT_TOKEN=your_bot_token
TG_CHAT_ID=your_chat_id

# 安全开关（默认False）
ENABLE_TRADING=False  # 设置为True以启用实际交易
```

### 4. 初始化数据库

```bash
python -c "from database import DatabaseManager; from config import ConfigManager; db = DatabaseManager(); db.init_database(); cm = ConfigManager(db); cm.init_default_configs()"
```

### 5. 运行

```bash
# 启动系统
python main.py
```

访问 Web界面: http://localhost:5000

## 项目结构

```
fundingRate/
├── config/              # 配置管理
├── core/                # 核心模块
│   ├── data_collector.py      # 数据采集器
│   ├── opportunity_monitor.py # 机会监控
│   ├── strategy_executor.py   # 策略执行
│   └── risk_manager.py        # 风险管理
├── strategies/          # 套利策略
├── exchanges/           # 交易所适配器
├── web/                 # Web界面
├── bot/                 # Telegram Bot
├── backtest/            # 回测系统
├── database/            # 数据库
├── utils/               # 工具函数
├── data/                # 数据存储
│   ├── database.db
│   ├── historical/      # 历史数据
│   └── backups/         # 备份
├── logs/                # 日志
├── docs/                # 文档
├── tests/               # 测试
├── main.py              # 主程序
├── requirements.txt     # 依赖
└── .env                 # 环境变量（不提交到git）
```

## 使用指南

### Web界面

1. **仪表盘** (`/`) - 查看总览和当前持仓
2. **机会监控** (`/opportunities`) - 实时机会排行榜
   - 收益排行
   - 资金费率排行
   - 基差排行
   - 综合评分
3. **持仓管理** (`/positions`) - 查看和管理持仓
4. **策略配置** (`/config`) - 配置策略参数
5. **回测系统** (`/backtest`) - 回测和参数优化
6. **数据管理** (`/data`) - 导入/导出数据
7. **系统设置** (`/settings`) - 交易所、通知等

### Telegram Bot命令

**查询**:
- `/balance` - 查看余额
- `/positions` - 查看持仓
- `/opportunities` - 当前机会
- `/status` - 系统状态
- `/report` - 今日报告

**控制**:
- `/pause` - 暂停所有策略
- `/resume` - 恢复策略
- `/close <ID>` - 平仓
- `/closeall` - 全部平仓

### 配置说明

#### 三层配置架构

1. **全局配置** - 应用于所有交易对
2. **策略配置** - 每个策略的默认参数
3. **交易对配置** - 针对单个交易对的精细配置（优先级最高）

#### 热更新配置

标注 ⚡ 的配置修改后立即生效，其他配置需要重启系统。

## 安全建议

1. **API权限**: 只授予"读取"和"交易"权限，不要授予"提现"权限
2. **测试模式**: 首次运行建议设置 `ENABLE_TRADING=False` 进行模拟
3. **小资金测试**: 从小资金开始，逐步增加
4. **监控告警**: 配置Telegram Bot接收实时通知
5. **定期备份**: 系统每日自动备份数据库到 `data/backups/`
6. **API密钥安全**: 不要将 `.env` 文件提交到git

## 风险提示

⚠️ **加密货币交易存在风险，请谨慎操作**

- 套利并非无风险，可能面临价格剧烈波动、流动性不足、交易所故障等风险
- 资金费率可能突然反转
- 基差可能继续扩大而非收敛
- 建议充分测试后再投入真实资金
- 建议设置合理的止损和仓位限制

## 部署到生产环境

### 使用systemd (Linux)

```bash
# 创建服务文件
sudo nano /etc/systemd/system/funding-arbitrage.service
```

```ini
[Unit]
Description=Funding Rate Arbitrage System
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/fundingRate
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# 启动服务
sudo systemctl daemon-reload
sudo systemctl enable funding-arbitrage
sudo systemctl start funding-arbitrage
sudo systemctl status funding-arbitrage
```

### 使用Docker

```bash
docker-compose up -d
```

## 开发

### 运行测试

```bash
pytest tests/
```

### 回测历史策略

1. 导入历史数据（CSV）到数据库
2. 在Web界面进入"回测系统"
3. 配置回测参数
4. 执行回测
5. 查看性能分析和图表

### 参数优化

在回测系统中使用"参数优化"功能，系统会自动网格搜索最佳参数组合。

## 常见问题

**Q: 为什么没有发现机会？**
A: 检查：1) 交易所API是否正确配置 2) 策略是否启用 3) 配置阈值是否过高

**Q: 如何增加新的交易对？**
A: 在Web界面"策略配置" -> "交易对配置" -> "添加新交易对"

**Q: 可以同时运行多个策略吗？**
A: 可以，三种策略可以同时运行，系统会智能分配资金

**Q: 数据库文件太大怎么办？**
A: 在"数据管理"中配置数据保留期限，定期清理旧数据

## 贡献

欢迎提交Issue和Pull Request！

## 许可证

MIT License

## 免责声明

本软件仅供学习和研究使用。使用本软件进行实际交易造成的任何损失，作者不承担责任。

---

**开发者**: Claude & User
**版本**: 1.0.0
**最后更新**: 2026-01-21
