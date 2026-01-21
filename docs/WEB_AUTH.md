# Web界面认证说明

## 默认凭据

- 用户名: `admin`
- 密码: `admin123`

**⚠️ 重要**: 请在生产环境中修改默认密码!

## 修改密码

### 方法1: 使用密码哈希生成工具

1. 运行密码哈希生成脚本:
```bash
python scripts/generate_password_hash.py
```

2. 输入新密码，脚本会生成密码哈希

3. 将生成的哈希值添加到 `.env` 文件:
```
WEB_PASSWORD_HASH=scrypt:32768:8:1$xxxxxxxxxxxxxxx
```

### 方法2: 使用Python命令

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('your_new_password'))"
```

将输出的哈希值添加到 `.env` 文件。

## 环境变量配置

在 `.env` 文件中配置以下变量:

```env
# Web认证配置
WEB_USERNAME=admin                    # 登录用户名
WEB_PASSWORD_HASH=scrypt:32768:8:1$... # 密码哈希
SECRET_KEY=your-secret-key-here        # Flask会话密钥
```

## 安全建议

1. **修改默认密码**: 不要在生产环境使用默认密码 `admin123`
2. **使用强密码**: 使用至少12位的强密码，包含大小写字母、数字和特殊字符
3. **更改SECRET_KEY**: 生成随机的SECRET_KEY用于Flask会话加密
4. **使用HTTPS**: 在生产环境中使用HTTPS协议
5. **限制访问**: 使用防火墙限制Web界面的访问IP

## 生成随机SECRET_KEY

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

将生成的值添加到 `.env` 文件:
```
SECRET_KEY=生成的随机字符串
```

## 会话管理

- 登录后会话将保持活跃状态
- 关闭浏览器后会话失效
- 点击"退出登录"可手动登出
- 所有API端点都需要认证

## 受保护的页面

以下页面需要登录后才能访问:

- `/` - 仪表盘
- `/opportunities` - 机会监控
- `/positions` - 持仓管理
- `/config` - 系统配置
- `/api/*` - 所有API端点(除了 `/api/status`)

## 故障排除

### 无法登录

1. 检查用户名和密码是否正确
2. 确认 `.env` 文件中的 `WEB_PASSWORD_HASH` 正确设置
3. 检查日志文件 `logs/app.log` 查看详细错误信息

### 会话频繁失效

1. 确认 `SECRET_KEY` 已正确设置
2. 检查浏览器是否禁用了Cookie
3. 确保应用程序没有频繁重启

### 忘记密码

1. 使用密码哈希生成工具生成新的密码哈希
2. 更新 `.env` 文件中的 `WEB_PASSWORD_HASH`
3. 重启应用程序
