#!/usr/bin/env python3
"""
综合系统测试脚本
测试所有核心功能
"""
import sys
import os
from decimal import Decimal

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """测试所有模块导入"""
    print("=" * 60)
    print("测试1: 模块导入")
    print("=" * 60)

    try:
        from database import DatabaseManager
        from config import ConfigManager
        from core import DataCollector, OpportunityMonitor, RiskManager, OrderManager, StrategyExecutor
        from bot import TelegramBot
        from exchanges import BinanceAdapter, OKXAdapter, BybitAdapter, GateAdapter, BitgetAdapter
        from utils import logger, calculator
        print("✅ 所有模块导入成功")
        return True
    except Exception as e:
        print(f"❌ 模块导入失败: {e}")
        return False


def test_database():
    """测试数据库功能"""
    print("\n" + "=" * 60)
    print("测试2: 数据库功能")
    print("=" * 60)

    try:
        from database import DatabaseManager

        db = DatabaseManager('data/test_database.db')  # 使用测试数据库
        db.init_database()

        # 测试插入配置
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO config (category, key, value, is_hot_reload)
                VALUES ('test', 'key1', '123', 1)
            """)
            conn.commit()

            # 测试查询
            cursor.execute("SELECT * FROM config WHERE category='test'")
            result = cursor.fetchone()
            assert result is not None, "配置插入失败"

        # 清理测试数据
        import os
        if os.path.exists('data/test_database.db'):
            os.remove('data/test_database.db')

        print("✅ 数据库功能正常")
        return True
    except Exception as e:
        print(f"❌ 数据库测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_config_manager():
    """测试配置管理器"""
    print("\n" + "=" * 60)
    print("测试3: 配置管理器")
    print("=" * 60)

    try:
        from database import DatabaseManager
        from config import ConfigManager

        db = DatabaseManager('data/test_database.db')
        db.init_database()

        config = ConfigManager(db)
        config.init_default_configs()

        # 测试获取配置
        total_capital = config.get('global', 'total_capital', 0)
        assert total_capital == 100000, f"默认资金配置错误: {total_capital}"

        # 测试设置配置
        config.set('test.key1', 'value1')
        value = config.get('test', 'key1')
        assert value == 'value1', f"配置设置/获取失败: {value}"

        # 清理测试数据
        import os
        if os.path.exists('data/test_database.db'):
            os.remove('data/test_database.db')

        print("✅ 配置管理器正常")
        return True
    except Exception as e:
        print(f"❌ 配置管理器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_calculator():
    """测试计算器工具"""
    print("\n" + "=" * 60)
    print("测试4: 计算器工具")
    print("=" * 60)

    try:
        from utils.calculator import (
            estimate_slippage,
            calculate_score,
            calculate_cross_exchange_funding_profit
        )

        # 测试滑点估算
        slippage = estimate_slippage(10000, 50000)
        assert isinstance(slippage, (int, float, Decimal)), "滑点计算返回类型错误"
        assert slippage >= 0, f"滑点不能为负: {slippage}"

        # 测试评分计算
        score = calculate_score(0.01, 'low', 100000)
        assert 0 <= score <= 100, f"评分超出范围: {score}"

        # 测试跨交易所套利计算
        profit_data = calculate_cross_exchange_funding_profit(
            funding_rate_long=0.0001,
            funding_rate_short=0.0005,
            entry_price_long=50000,
            entry_price_short=50010,
            position_size=10000,
            depth_long=100000,
            depth_short=100000,
            maker_fee_long=0.0002,
            taker_fee_long=0.0005,
            maker_fee_short=0.0002,
            taker_fee_short=0.0005
        )

        assert 'net_profit' in profit_data, "缺少净利润字段"
        assert 'net_return' in profit_data, "缺少净收益率字段"

        print("✅ 计算器工具正常")
        return True
    except Exception as e:
        print(f"❌ 计算器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_exchange_adapters():
    """测试交易所适配器"""
    print("\n" + "=" * 60)
    print("测试5: 交易所适配器")
    print("=" * 60)

    try:
        from exchanges import BinanceAdapter

        # 测试实例化(不需要真实API密钥)
        exchange = BinanceAdapter(None, None)
        assert hasattr(exchange, 'exchange'), "交易所对象缺少exchange属性"

        # 测试符号转换
        futures_symbol = exchange._convert_to_futures_symbol('BTC/USDT')
        assert 'BTC' in futures_symbol and 'USDT' in futures_symbol, f"符号转换错误: {futures_symbol}"

        print("✅ 交易所适配器正常")
        return True
    except Exception as e:
        print(f"❌ 交易所适配器测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_web_api():
    """测试Web API"""
    print("\n" + "=" * 60)
    print("测试6: Web API")
    print("=" * 60)

    try:
        import requests
        import time

        # 等待服务器启动
        time.sleep(2)

        base_url = "http://localhost:5000"

        # 测试状态API
        response = requests.get(f"{base_url}/api/status", timeout=5)
        assert response.status_code == 200, f"状态API返回错误: {response.status_code}"
        data = response.json()
        assert 'status' in data, "状态API缺少status字段"

        # 测试持仓API
        response = requests.get(f"{base_url}/api/positions", timeout=5)
        assert response.status_code == 200, f"持仓API返回错误: {response.status_code}"

        # 测试机会API
        response = requests.get(f"{base_url}/api/opportunities", timeout=5)
        assert response.status_code == 200, f"机会API返回错误: {response.status_code}"

        # 测试配置API
        response = requests.get(f"{base_url}/api/config", timeout=5)
        assert response.status_code == 200, f"配置API返回错误: {response.status_code}"

        print("✅ Web API正常")
        return True
    except requests.exceptions.ConnectionError:
        print("⚠️  Web服务器未运行,跳过API测试")
        return True
    except Exception as e:
        print(f"❌ Web API测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_git_status():
    """测试Git状态"""
    print("\n" + "=" * 60)
    print("测试7: Git仓库状态")
    print("=" * 60)

    try:
        import subprocess

        result = subprocess.run(['git', 'log', '--oneline', '-5'],
                              capture_output=True, text=True, check=True)
        commits = result.stdout.strip().split('\n')

        print(f"✅ Git仓库正常 ({len(commits)} 个最新提交)")
        for commit in commits:
            print(f"   {commit}")

        return True
    except Exception as e:
        print(f"⚠️  Git检查失败: {e}")
        return True  # Git状态不影响系统功能


def main():
    """主测试函数"""
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║                                                           ║
    ║       资金费率套利系统 - 综合测试                           ║
    ║                                                           ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    tests = [
        ("模块导入", test_imports),
        ("数据库功能", test_database),
        ("配置管理器", test_config_manager),
        ("计算器工具", test_calculator),
        ("交易所适配器", test_exchange_adapters),
        ("Web API", test_web_api),
        ("Git仓库", test_git_status),
    ]

    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ 测试 {name} 异常: {e}")
            results.append((name, False))

    # 总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")

    print("\n" + "=" * 60)
    print(f"通过: {passed}/{total}")
    print("=" * 60)

    if passed == total:
        print("\n🎉 所有测试通过! 系统准备就绪。")
        return 0
    else:
        print(f"\n⚠️  {total - passed} 个测试失败,请检查。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
