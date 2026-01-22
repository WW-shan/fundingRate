# 快速启动指南

## 🚀 5分钟快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件（最少配置）：

```env
# 数据库路径
DATABASE_PATH=data/database.db

# Web界面密钥（生产环境必须修改！）
SECRET_KEY=your-secret-key-change-me

# Web登录（默认: admin / admin123）
WEB_USERNAME=admin
# 生成密码哈希: python scripts/generate_password_hash.py
WEB_PASSWORD_HASH=

# 交易模式（False=模拟，True=实盘）
ENABLE_TRADING=False
```

### 3. 运行系统

```bash
python main.py
```

或使用Docker：

```bash
docker-compose up -d
```

### 4. 访问Web界面

打开浏览器访问: http://localhost:5000

默认登录:
- 用户名: `admin`
- 密码: `admin123`

## 📋 已修复的Bug

### Bug #1: Missing Any import ✅

**错误信息**:
```
NameError: name 'Any' is not defined
File: backtesting/data_loader.py, line 113
```

**修复**: 添加 `Any` 到 typing 导入
```python
from typing import Dict, List, Optional, Any
```

**状态**: ✅ 已修复并提交

### Bug #2: StrategyExecutor pause/resume ✅

**状态**: ✅ 已修复并提交

### Bug #3: TelegramBot initialization ✅

**状态**: ✅ 已修复并提交

## ✅ 系统状态

当前系统状态：**可运行** 🎉

- ✅ 所有语法错误已修复
- ✅ 所有导入问题已解决
- ✅ 暂停/恢复功能已实现
- ✅ Web界面已优化
- ✅ 性能优化已完成

## 🧪 测试系统

### 快速测试

```bash
# 语法检查
find . -name "*.py" -not -path "./venv/*" | xargs python3 -m py_compile

# 数据库测试
python3 -c "from database import DatabaseManager; db = DatabaseManager(); db.init_database(); print('✓ Database OK')"
```

### 完整测试

```bash
# 运行测试套件（如果有）
pytest tests/

# 或手动测试各个组件
python3 -c "
from database import DatabaseManager
from config import ConfigManager

db = DatabaseManager()
db.init_database()
config = ConfigManager(db)
config.init_default_configs()
print('✓ All components initialized successfully')
"
```

## 📊 系统架构

```
┌─────────────────────────────────────────┐
│         Web Interface (Flask)           │
│         http://localhost:5000           │
└──────────────┬──────────────────────────┘
               │
┌──────────────┴──────────────────────────┐
│         Main System                     │
│  - Data Collector                       │
│  - Opportunity Monitor                  │
│  - Strategy Executor                    │
│  - Risk Manager                         │
└──────────────┬──────────────────────────┘
               │
┌──────────────┴──────────────────────────┐
│         SQLite Database                 │
│         data/database.db                │
└─────────────────────────────────────────┘
```

## 🔧 常用命令

### 查看日志

```bash
tail -f logs/app.log
```

### 生成密码哈希

```bash
python scripts/generate_password_hash.py
```

### 数据库备份

```bash
cp data/database.db data/backups/backup_$(date +%Y%m%d_%H%M%S).db
```

### 运行回测

```bash
python scripts/run_backtest.py --start 2024-01-01 --end 2024-12-31 --capital 100000
```

## 🐛 遇到问题？

### 1. 依赖安装失败

```bash
# 升级pip
pip install --upgrade pip

# 使用国内镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 端口被占用

修改 `.env`:
```env
WEB_PORT=5001
```

### 3. 数据库错误

```bash
# 重新初始化
rm -f data/database.db
python -c "from database import DatabaseManager; db = DatabaseManager(); db.init_database()"
```

### 4. 查看详细错误

修改 `.env`:
```env
LOG_LEVEL=DEBUG
```

## 📚 更多文档

- [完整设计文档](docs/plans/2026-01-21-funding-rate-arbitrage-design.md)
- [Web认证指南](docs/WEB_AUTH.md)
- [回测系统指南](docs/BACKTEST_GUIDE.md)
- [性能优化指南](docs/PERFORMANCE_OPTIMIZATION.md)
- [调试指南](docs/DEBUGGING.md)

## 🎯 下一步

1. ✅ 配置交易所API密钥（在.env中）
2. ✅ 修改默认Web登录密码
3. ✅ 配置Telegram Bot（可选）
4. ✅ 启动系统测试
5. ✅ 监控系统运行状态

## 💡 提示

- 首次运行建议使用 `ENABLE_TRADING=False` 模拟模式
- 生产环境务必修改 `SECRET_KEY` 和 `WEB_PASSWORD_HASH`
- 定期检查 `logs/app.log` 日志文件
- 使用 `/api/health` 端点监控系统健康状态

---

**系统已就绪，开始交易吧！** 🚀
