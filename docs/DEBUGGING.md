# 系统运行和调试指南

## 快速检查

运行系统检查脚本:

```bash
python3 check_system.py
```

此脚本会检查:
- ✓ Python 版本
- ✓ 必要文件存在性
- ✓ 代码语法
- ✓ 模块结构
- ✓ 配置文件
- ✓ 数据目录
- ✓ 依赖包配置
- ✓ 关键方法
- ✓ Web模板
- ✓ Docker配置

## 常见问题修复

### 1. 模块导入错误

**问题**: `No module named 'loguru'` 或其他模块

**解决方案**:
```bash
pip install -r requirements.txt
```

### 2. 权限错误

**问题**: 无法创建data或logs目录

**解决方案**:
```bash
mkdir -p data logs data/backups data/historical
chmod 755 data logs
```

### 3. 数据库初始化失败

**问题**: SQLite数据库无法创建

**解决方案**:
```bash
rm -f data/database.db
python3 -c "from database import DatabaseManager; db = DatabaseManager(); db.init_database()"
```

### 4. Web界面无法访问

**问题**: 端口被占用或防火墙阻止

**解决方案**:
```bash
# 检查端口
lsof -i :5000

# 更改端口（在.env中）
WEB_PORT=5001
```

### 5. TelegramBot初始化失败

**问题**: `TelegramBot.__init__() missing required positional argument`

**解决方案**: 已修复，确保使用最新代码
```bash
git pull
```

## 调试模式

### 启用详细日志

编辑 `.env`:
```
LOG_LEVEL=DEBUG
```

### 查看日志

```bash
# 实时查看
tail -f logs/app.log

# 查看错误
grep ERROR logs/app.log

# 查看警告
grep WARN logs/app.log
```

## 测试各个组件

### 测试数据库

```python
from database import DatabaseManager

db = DatabaseManager()
db.init_database()
print("Database initialized successfully")
```

### 测试配置管理

```python
from config import ConfigManager
from database import DatabaseManager

db = DatabaseManager()
config = ConfigManager(db)
config.init_default_configs()
print("Config initialized successfully")
```

### 测试Web应用

```python
from web.app import create_app
from database import DatabaseManager
from config import ConfigManager

db = DatabaseManager()
config = ConfigManager(db)
app = create_app(config, db, None, None, None, None)
print("Web app created successfully")
```

## 性能监控

### 检查系统状态

```bash
curl http://localhost:5000/api/health
```

### 检查错误统计

```bash
curl http://localhost:5000/api/errors
```

### 查看系统指标

访问: `http://localhost:5000/api/status`

## Docker调试

### 查看容器日志

```bash
docker-compose logs -f
```

### 进入容器

```bash
docker-compose exec funding-arbitrage bash
```

### 重建容器

```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

## 代码质量检查

### 语法检查

```bash
find . -name "*.py" -not -path "./venv/*" | xargs python3 -m py_compile
```

### 代码格式化

```bash
# 安装black（可选）
pip install black

# 格式化代码
black .
```

## 备份和恢复

### 手动备份数据库

```bash
cp data/database.db data/backups/database_$(date +%Y%m%d_%H%M%S).db
```

### 恢复数据库

```bash
cp data/backups/database_20240101_120000.db data/database.db
```

## 已修复的Bug

✅ **TelegramBot初始化** - 添加了opportunity_monitor参数
✅ **StrategyExecutor暂停功能** - 添加了set_paused方法
✅ **Performance模块** - psutil设为可选依赖
✅ **Web API引号转义** - 修复了logger语句
✅ **仪表盘优化** - 完全重写，现代化设计

## 性能优化建议

1. **数据库连接池**: 已实现，配置在 `utils/db_optimization.py`
2. **查询缓存**: 已实现，可在代码中启用
3. **批量写入**: 已实现，用于高频数据插入
4. **异步处理**: TelegramBot已使用异步
5. **监控告警**: 通过/api/health端点

## 生产环境部署

### 必须修改的配置

```env
# .env 文件
SECRET_KEY=<生成随机密钥>
WEB_PASSWORD_HASH=<使用scripts/generate_password_hash.py生成>
ENABLE_TRADING=True  # 启用实盘交易

# 交易所API密钥
BINANCE_API_KEY=<真实密钥>
BINANCE_API_SECRET=<真实密钥>
# ... 其他交易所
```

### 安全检查清单

- [ ] 修改默认密码
- [ ] 设置强SECRET_KEY
- [ ] 配置防火墙规则
- [ ] 启用HTTPS（反向代理）
- [ ] 限制IP访问
- [ ] 定期备份数据库
- [ ] 监控日志文件大小
- [ ] 设置告警通知

## 获取帮助

如遇问题，请检查:
1. 日志文件: `logs/app.log`
2. 系统状态: `http://localhost:5000/api/health`
3. 错误统计: `http://localhost:5000/api/errors`
4. 运行检查: `python3 check_system.py`
