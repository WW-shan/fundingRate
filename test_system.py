#!/usr/bin/env python3
"""
系统测试脚本
验证所有模块是否正确安装和配置
"""
import os
import sys

def test_imports():
    """测试所有模块导入"""
    print("🔍 Testing imports...")

    try:
        from database import DatabaseManager
        print("✅ database.DatabaseManager")

        from config import ConfigManager
        print("✅ config.ConfigManager")

        from exchanges import BinanceAdapter, OKXAdapter, BybitAdapter, GateAdapter, BitgetAdapter
        print("✅ exchanges.*")

        from utils import calculator, setup_logger
        print("✅ utils.*")

        from core import DataCollector, OpportunityMonitor, RiskManager, OrderManager, StrategyExecutor
        print("✅ core.*")

        from bot import TelegramBot
        print("✅ bot.TelegramBot")

        print("\n✅ All imports successful!")
        return True

    except Exception as e:
        print(f"\n❌ Import failed: {e}")
        return False

def test_database():
    """测试数据库初始化"""
    print("\n🔍 Testing database...")

    try:
        from database import DatabaseManager

        db = DatabaseManager("data/test_database.db")
        db.init_database()

        # 测试配置
        db.set_config('test', 'key', 'value')
        value = db.get_config('test', 'key')

        assert value == 'value', "Config test failed"

        print("✅ Database initialization successful!")

        # 清理测试数据库
        if os.path.exists("data/test_database.db"):
            os.remove("data/test_database.db")

        return True

    except Exception as e:
        print(f"❌ Database test failed: {e}")
        return False

def test_config():
    """测试配置管理"""
    print("\n🔍 Testing config manager...")

    try:
        from database import DatabaseManager
        from config import ConfigManager

        db = DatabaseManager("data/test_database.db")
        db.init_database()

        config = ConfigManager(db)
        config.init_default_configs()

        # 测试读取配置
        value = config.get('global', 'total_capital')
        assert value is not None, "Config read failed"

        print("✅ Config manager successful!")

        # 清理
        if os.path.exists("data/test_database.db"):
            os.remove("data/test_database.db")

        return True

    except Exception as e:
        print(f"❌ Config test failed: {e}")
        return False

def check_env():
    """检查环境变量"""
    print("\n🔍 Checking environment variables...")

    env_file = ".env"
    if not os.path.exists(env_file):
        print(f"⚠️  .env file not found (copy from .env.example)")
        print("   cp .env.example .env")
        return False

    print("✅ .env file exists")

    # 检查关键配置
    from dotenv import load_dotenv
    load_dotenv()

    enable_trading = os.getenv('ENABLE_TRADING', 'False')
    print(f"   ENABLE_TRADING: {enable_trading}")

    if enable_trading.lower() == 'true':
        print("⚠️  Trading is ENABLED - make sure API keys are configured")
    else:
        print("✅ Trading is DISABLED (simulation mode)")

    return True

def main():
    """主测试函数"""
    print("=" * 60)
    print("Funding Rate Arbitrage System - Test Suite")
    print("=" * 60)

    results = []

    # 运行测试
    results.append(("Imports", test_imports()))
    results.append(("Database", test_database()))
    results.append(("Config", test_config()))
    results.append(("Environment", check_env()))

    # 总结
    print("\n" + "=" * 60)
    print("Test Results:")
    print("=" * 60)

    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{test_name:20} {status}")

    all_passed = all(result for _, result in results)

    print("=" * 60)
    if all_passed:
        print("✅ All tests passed! System is ready.")
        print("\nNext steps:")
        print("1. Configure API keys in .env file")
        print("2. Run: python main.py")
        return 0
    else:
        print("❌ Some tests failed. Please fix the issues above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
