"""
测试配置持久化
验证配置在重启后是否保持
"""
import sys
sys.path.insert(0, '.')

from database.db_manager import DatabaseManager
from config.config_manager import ConfigManager
from loguru import logger


def test_config_persistence():
    """测试配置持久化"""
    logger.info("=" * 70)
    logger.info("测试配置持久化功能")
    logger.info("=" * 70)
    
    db = DatabaseManager()
    
    # 第一次初始化
    logger.info("\n【步骤 1】首次初始化配置")
    config1 = ConfigManager(db)
    config1.init_default_configs()
    
    # 读取默认值
    total_capital = config1.get('global', 'total_capital')
    max_positions = config1.get('global', 'max_positions')
    logger.info(f"初始配置:")
    logger.info(f"  total_capital: {total_capital}")
    logger.info(f"  max_positions: {max_positions}")
    
    # 修改配置
    logger.info("\n【步骤 2】修改配置")
    new_capital = 200000
    new_positions = 20
    config1.set('global', 'total_capital', new_capital)
    config1.set('global', 'max_positions', new_positions)
    logger.info(f"修改后:")
    logger.info(f"  total_capital: {config1.get('global', 'total_capital')}")
    logger.info(f"  max_positions: {config1.get('global', 'max_positions')}")
    
    # 模拟重启 - 创建新的 ConfigManager 实例
    logger.info("\n【步骤 3】模拟重启（创建新实例）")
    config2 = ConfigManager(db)
    config2.init_default_configs()
    
    # 读取配置，应该是修改后的值
    loaded_capital = config2.get('global', 'total_capital')
    loaded_positions = config2.get('global', 'max_positions')
    logger.info(f"重启后读取:")
    logger.info(f"  total_capital: {loaded_capital}")
    logger.info(f"  max_positions: {loaded_positions}")
    
    # 验证
    logger.info("\n【步骤 4】验证结果")
    if loaded_capital == new_capital and loaded_positions == new_positions:
        logger.success("✅ 配置持久化成功！修改的配置在重启后保持不变")
    else:
        logger.error("❌ 配置持久化失败！")
        logger.error(f"  期望 total_capital: {new_capital}, 实际: {loaded_capital}")
        logger.error(f"  期望 max_positions: {new_positions}, 实际: {loaded_positions}")
        return False
    
    # 测试默认配置不会覆盖
    logger.info("\n【步骤 5】测试 set_default 不覆盖已有配置")
    config2.set_default('global', 'total_capital', 999999)
    final_capital = config2.get('global', 'total_capital')
    
    if final_capital == new_capital:
        logger.success(f"✅ set_default 正确工作，配置保持为 {final_capital}，未被覆盖为 999999")
    else:
        logger.error(f"❌ set_default 错误覆盖了配置！")
        return False
    
    logger.info("\n" + "=" * 70)
    logger.success("所有测试通过！配置持久化功能正常 ✅")
    logger.info("=" * 70)
    
    return True


if __name__ == "__main__":
    try:
        success = test_config_persistence()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.error(f"测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
