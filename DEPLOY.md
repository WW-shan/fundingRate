# 部署指南

## 推送到GitHub

仓库已创建并完成本地提交，现在需要推送到GitHub。

### 方法1：使用Personal Access Token（推荐）

1. **创建GitHub Personal Access Token**
   - 访问：https://github.com/settings/tokens
   - 点击 "Generate new token" -> "Generate new token (classic)"
   - Token名称：`fundingRate-deploy`
   - 权限选择：勾选 `repo`（完整的仓库访问权限）
   - 点击 "Generate token"
   - **复制生成的token**（只显示一次，请保存好）

2. **推送代码**
   ```bash
   # 将 YOUR_TOKEN 替换为你刚才复制的token
   git push https://YOUR_TOKEN@github.com/WW-shan/fundingRate.git main
   ```

3. **（可选）保存凭证**
   ```bash
   # 配置凭证存储（避免每次都输入token）
   git config credential.helper store
   git push -u origin main
   # 输入 username: WW-shan
   # 输入 password: YOUR_TOKEN
   ```

### 方法2：使用SSH密钥（长期使用推荐）

1. **生成SSH密钥**（如果还没有）
   ```bash
   ssh-keygen -t ed25519 -C "212500581@qq.com"
   # 按Enter使用默认路径
   # 可以设置密码或直接按Enter
   ```

2. **添加SSH密钥到GitHub**
   ```bash
   # 查看公钥
   cat ~/.ssh/id_ed25519.pub
   # 复制输出的公钥
   ```
   - 访问：https://github.com/settings/keys
   - 点击 "New SSH key"
   - Title: `fundingRate-server`
   - Key: 粘贴刚才复制的公钥
   - 点击 "Add SSH key"

3. **修改remote URL并推送**
   ```bash
   git remote set-url origin git@github.com:WW-shan/fundingRate.git
   git push -u origin main
   ```

## 验证推送成功

推送成功后，访问：https://github.com/WW-shan/fundingRate

你应该能看到：
- ✅ 45个文件
- ✅ README.md 显示在首页
- ✅ 完整的项目结构
- ✅ 提交信息："Initial commit: Funding Rate Arbitrage System"

## 下一步

推送成功后，你可以：

1. **在其他机器上克隆项目**
   ```bash
   git clone https://github.com/WW-shan/fundingRate.git
   cd fundingRate
   ```

2. **继续开发**
   - 实现核心业务逻辑模块
   - 开发Web界面
   - 添加Telegram Bot功能
   - 完善回测系统

3. **设置GitHub仓库**
   - 添加描述
   - 添加Topics标签：`cryptocurrency`, `arbitrage`, `trading-bot`, `python`
   - 设置为Private（如果不想公开）

## 故障排除

### 问题1：推送时要求输入密码

**原因**：GitHub已经不支持密码认证，必须使用Personal Access Token或SSH。

**解决**：使用上面的方法1或方法2。

### 问题2：推送时提示"Permission denied"

**原因**：没有仓库写入权限或认证失败。

**解决**：
- 确认token有正确的权限
- 确认username正确（WW-shan）
- 确认token没有过期

### 问题3：推送时提示"repository not found"

**原因**：仓库URL不正确或仓库不存在。

**解决**：
- 确认仓库已在GitHub上创建
- 检查URL拼写：https://github.com/WW-shan/fundingRate.git

## 当前项目状态

✅ **已完成**：
- 设计文档（docs/plans/2026-01-21-funding-rate-arbitrage-design.md）
- 数据库层（database/）
- 配置管理层（config/）
- 交易所适配器（exchanges/）
- 工具函数（utils/）
- 项目结构和README
- Git仓库初始化和提交

⏳ **待实现**：
- 数据采集器（core/data_collector.py）
- 机会监控系统（core/opportunity_monitor.py）
- 三种套利策略（strategies/）
- 风险管理器（core/risk_manager.py）
- 策略执行引擎（core/strategy_executor.py）
- Flask Web应用（web/）
- Telegram Bot（bot/）
- 回测系统（backtest/）

---

**Git仓库信息**：
- 仓库地址：https://github.com/WW-shan/fundingRate.git
- 分支：main
- 最新提交：62c0ef9
- 文件数量：45
- 代码行数：2314+
