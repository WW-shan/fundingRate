#!/usr/bin/env python3
"""
系统运行检查脚本
检查所有关键组件是否正常工作
"""

import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 60)
print("资金费率套利系统 - 运行检查")
print("=" * 60)
print()

# 颜色定义
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'

def check(name, func):
    """执行检查并打印结果"""
    try:
        result = func()
        if result:
            print(f"{GREEN}✓{RESET} {name}")
            return True
        else:
            print(f"{RED}✗{RESET} {name}")
            return False
    except Exception as e:
        print(f"{RED}✗{RESET} {name}: {e}")
        return False

# 检查1: Python版本
def check_python_version():
    version = sys.version_info
    if version.major == 3 and version.minor >= 10:
        return True
    print(f"  需要 Python 3.10+，当前版本: {version.major}.{version.minor}")
    return False

# 检查2: 必要的文件
def check_files():
    required_files = [
        'main.py',
        'requirements.txt',
        '.env.example',
        'database/__init__.py',
        'config/__init__.py',
        'core/__init__.py',
        'bot/__init__.py',
        'web/app.py'
    ]
    for file in required_files:
        if not os.path.exists(file):
            print(f"  缺少文件: {file}")
            return False
    return True

# 检查3: 语法检查
def check_syntax():
    import py_compile
    import glob

    py_files = glob.glob('**/*.py', recursive=True)
    py_files = [f for f in py_files if not f.startswith(('venv/', '__pycache__/', 'tests/'))]

    errors = []
    for file in py_files[:20]:  # 只检查前20个
        try:
            py_compile.compile(file, doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"{file}: {e}")

    if errors:
        print(f"  发现 {len(errors)} 个语法错误")
        for err in errors[:3]:
            print(f"    - {err}")
        return False
    return True

# 检查4: 模块导入（不需要依赖）
def check_module_structure():
    """检查模块结构是否正确"""
    modules = {
        'database': '__init__.py',
        'config': '__init__.py',
        'core': '__init__.py',
        'bot': '__init__.py',
        'web': 'app.py',
        'utils': '__init__.py',
        'backtesting': '__init__.py'
    }

    for module, file in modules.items():
        path = os.path.join(module, file)
        if not os.path.exists(path):
            print(f"  缺少: {path}")
            return False
    return True

# 检查5: 配置文件
def check_config():
    if not os.path.exists('.env.example'):
        return False

    with open('.env.example', 'r') as f:
        content = f.read()

    required_vars = [
        'BINANCE_API_KEY',
        'TG_BOT_TOKEN',
        'WEB_USERNAME',
        'SECRET_KEY',
        'DATABASE_PATH'
    ]

    for var in required_vars:
        if var not in content:
            print(f"  .env.example 缺少: {var}")
            return False
    return True

# 检查6: 数据库目录
def check_directories():
    dirs = ['data', 'logs', 'data/backups', 'data/historical']
    for dir in dirs:
        if not os.path.exists(dir):
            try:
                os.makedirs(dir, exist_ok=True)
            except:
                print(f"  无法创建目录: {dir}")
                return False
    return True

# 检查7: requirements.txt
def check_requirements():
    if not os.path.exists('requirements.txt'):
        return False

    with open('requirements.txt', 'r') as f:
        content = f.read()

    required_packages = [
        'Flask',
        'ccxt',
        'python-telegram-bot',
        'loguru',
        'pandas',
        'psutil'
    ]

    for pkg in required_packages:
        if pkg not in content:
            print(f"  requirements.txt 缺少: {pkg}")
            return False
    return True

# 检查8: 关键方法存在性
def check_critical_methods():
    """检查关键方法是否存在"""
    checks = []

    # 检查TelegramBot.__init__参数
    try:
        with open('bot/telegram_bot.py', 'r') as f:
            content = f.read()
            if 'opportunity_monitor=None' in content:
                checks.append(True)
            else:
                print("  TelegramBot缺少opportunity_monitor参数")
                checks.append(False)
    except:
        checks.append(False)

    # 检查StrategyExecutor.set_paused
    try:
        with open('core/strategy_executor.py', 'r') as f:
            content = f.read()
            if 'def set_paused' in content:
                checks.append(True)
            else:
                print("  StrategyExecutor缺少set_paused方法")
                checks.append(False)
    except:
        checks.append(False)

    return all(checks)

# 检查9: Web模板
def check_templates():
    templates = [
        'web/templates/index.html',
        'web/templates/login.html',
        'web/templates/positions.html',
        'web/templates/opportunities.html',
        'web/templates/config.html'
    ]

    for tpl in templates:
        if not os.path.exists(tpl):
            print(f"  缺少模板: {tpl}")
            return False
    return True

# 检查10: Docker配置
def check_docker():
    if not os.path.exists('Dockerfile'):
        print("  缺少 Dockerfile")
        return False
    if not os.path.exists('docker-compose.yml'):
        print("  缺少 docker-compose.yml")
        return False
    return True

print("执行检查...")
print()

results = []
results.append(check("Python 版本 (3.10+)", check_python_version))
results.append(check("必要文件存在", check_files))
results.append(check("代码语法检查", check_syntax))
results.append(check("模块结构检查", check_module_structure))
results.append(check("配置文件检查", check_config))
results.append(check("数据目录检查", check_directories))
results.append(check("依赖包配置检查", check_requirements))
results.append(check("关键方法检查", check_critical_methods))
results.append(check("Web模板检查", check_templates))
results.append(check("Docker配置检查", check_docker))

print()
print("=" * 60)
passed = sum(results)
total = len(results)

if passed == total:
    print(f"{GREEN}✓ 所有检查通过 ({passed}/{total}){RESET}")
    print()
    print("系统准备就绪！")
    print()
    print("下一步:")
    print("1. 复制 .env.example 到 .env 并配置")
    print("2. 安装依赖: pip install -r requirements.txt")
    print("3. 运行系统: python main.py")
    print("   或使用Docker: docker-compose up -d")
    sys.exit(0)
else:
    print(f"{RED}✗ {total - passed} 个检查失败 ({passed}/{total}){RESET}")
    print()
    print("请修复上述问题后再运行系统")
    sys.exit(1)
