"""
配置管理器
支持三层配置：全局配置、策略配置、交易对配置
支持热更新
"""
import json
from typing import Any, Optional, Dict
from loguru import logger
from database.db_manager import DatabaseManager


class ConfigManager:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self._config_cache = {}
        self._load_all_configs()

    def _load_all_configs(self):
        """从数据库加载所有配置到缓存"""
        logger.info("Loading all configurations...")
        configs = self.db.execute_query("SELECT * FROM config")
        for cfg in configs:
            key = f"{cfg['category']}.{cfg['key']}"
            self._config_cache[key] = cfg['value']
        logger.info(f"Loaded {len(configs)} configurations")

    def get(self, category: str, key: str, default: Any = None) -> Any:
        """获取配置值"""
        cache_key = f"{category}.{key}"
        value = self._config_cache.get(cache_key)

        if value is None:
            return default

        # 尝试解析JSON
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    def set(self, category: str, key: str, value: Any,
            is_hot_reload: bool = True, description: str = ""):
        """设置配置值"""
        # 统一使用JSON序列化，确保类型正确（尤其是布尔值）
        if isinstance(value, (dict, list, bool, int, float, type(None))):
            value_str = json.dumps(value)
        else:
            value_str = str(value)

        self.db.set_config(category, key, value_str, is_hot_reload, description)

        # 更新缓存
        cache_key = f"{category}.{key}"
        self._config_cache[cache_key] = value_str

        logger.info(f"Config updated: {category}.{key} = {value}")
    
    def set_default(self, category: str, key: str, value: Any,
                   is_hot_reload: bool = True, description: str = ""):
        """设置默认配置值（如果配置已存在则不覆盖）"""
        cache_key = f"{category}.{key}"
        
        # 检查配置是否已存在
        if cache_key in self._config_cache:
            # 配置已存在，不覆盖
            return
        
        # 配置不存在，设置默认值
        self.set(category, key, value, is_hot_reload, description)

    def reload_hot_configs(self):
        """重新加载所有支持热更新的配置"""
        logger.info("Reloading hot configurations...")
        configs = self.db.execute_query(
            "SELECT * FROM config WHERE is_hot_reload = TRUE"
        )
        for cfg in configs:
            key = f"{cfg['category']}.{cfg['key']}"
            self._config_cache[key] = cfg['value']
        logger.info(f"Reloaded {len(configs)} hot configurations")

    def get_pair_config(self, symbol: str, exchange: Optional[str] = None,
                       strategy_prefix: Optional[str] = None) -> Dict[str, Any]:
        """
        获取交易对配置
        优先级：交易对配置 > 策略默认配置 > 全局配置
        """
        # 从数据库获取交易对配置
        if exchange:
            pair_configs = self.db.execute_query(
                "SELECT * FROM trading_pair_configs WHERE symbol = ? AND exchange = ?",
                (symbol, exchange)
            )
        else:
            pair_configs = self.db.execute_query(
                "SELECT * FROM trading_pair_configs WHERE symbol = ?",
                (symbol,)
            )

        if pair_configs:
            config = pair_configs[0]
        else:
            # 如果没有交易对配置，使用默认值
            config = self._get_default_pair_config(symbol, exchange)

        return config

    def _get_default_pair_config(self, symbol: str, exchange: Optional[str]) -> Dict[str, Any]:
        """获取交易对的默认配置"""
        return {
            'symbol': symbol,
            'exchange': exchange,
            'strategy1_enabled': False,
            'strategy2a_enabled': False,
            'strategy2b_enabled': False,
            's1_execution_mode': 'auto',
            's1_min_funding_diff': self.get('strategy1', 'min_funding_diff', 0.0005),
            's1_position_size': self.get('strategy1', 'position_size', 10),
            's1_target_exchanges': ['binance', 'okx', 'bybit'],
            's2a_execution_mode': 'auto',
            's2a_min_funding_rate': self.get('strategy2a', 'min_funding_rate', 0.0005),  # 单次费率0.05%
            's2a_position_size': self.get('strategy2a', 'position_size', 10),
            's2a_max_basis_deviation': self.get('strategy2a', 'max_basis_deviation', 0.01),
            's2b_execution_mode': 'manual',
            's2b_min_basis': self.get('strategy2b', 'min_basis', 0.02),
            's2b_position_size': self.get('strategy2b', 'position_size', 8),
            's3_enabled': self.get('strategy3', 'enabled', True),
            's3_min_funding_rate': self.get('strategy3', 'min_funding_rate', 0.0001),
            's3_position_size': self.get('strategy3', 'position_size', 10),
            's3_stop_loss_pct': self.get('strategy3', 'stop_loss_pct', 0.05),
            's3_check_basis': self.get('strategy3', 'check_basis', False),
            's3_short_exit_threshold': self.get('strategy3', 'short_exit_threshold', 0.0),
            's3_long_exit_threshold': self.get('strategy3', 'long_exit_threshold', 0.0),
            's3_trailing_stop_enabled': self.get('strategy3', 'trailing_stop_enabled', True),
            's3_trailing_activation_pct': self.get('strategy3', 'trailing_activation_pct', 0.04),
            's3_trailing_callback_pct': self.get('strategy3', 'trailing_callback_pct', 0.04),
            'max_positions': 3,
            'priority': 5,
            'is_active': True
        }

    def init_default_configs(self):
        """初始化默认配置（不覆盖已有配置）"""
        logger.info("Initializing default configurations...")

        # 全局配置
        self.set_default('global', 'total_capital', 100, True, "总资金池（USDT）")
        self.set_default('global', 'max_capital_usage', 0.8, True, "最大资金使用率")
        self.set_default('global', 'max_positions', 10, True, "最大同时持仓数")
        self.set_default('global', 'price_refresh_interval', 5, True, "价格刷新间隔（秒）")
        self.set_default('global', 'funding_refresh_interval', 300, True, "资金费率刷新间隔（秒）")
        self.set_default('global', 'opportunity_scan_interval', 10, True, "机会扫描间隔（秒）")

        # 策略1：跨交易所资金费率套利
        self.set_default('strategy1', 'enabled', False, True, "是否启用")
        self.set_default('strategy1', 'execution_mode', 'auto', True, "执行模式（auto/manual）")
        self.set_default('strategy1', 'position_size', 10, True, "默认开仓金额（USDT）")
        self.set_default('strategy1', 'min_funding_diff', 0.0005, True, "最小费率差（单期，0.05%）")
        self.set_default('strategy1', 'max_price_diff', 0.02, True, "最大价差容忍（2%）")
        self.set_default('strategy1', 'max_position_size', 15, True, "单笔最大仓位（USDT）")

        # 策略2A：现货期货资金费率套利
        self.set_default('strategy2a', 'enabled', False, True, "是否启用")
        self.set_default('strategy2a', 'execution_mode', 'auto', True, "执行模式")
        self.set_default('strategy2a', 'position_size', 10, True, "默认开仓金额（USDT）")
        self.set_default('strategy2a', 'min_funding_rate', 0.0005, True, "最小资金费率（单次，0.05%）")
        self.set_default('strategy2a', 'max_basis_deviation', 0.01, True, "基差安全范围（1%）")
        self.set_default('strategy2a', 'max_position_size', 15, True, "单笔最大仓位（USDT）")

        # 策略2B：基差套利
        self.set_default('strategy2b', 'enabled', False, True, "是否启用")
        self.set_default('strategy2b', 'execution_mode', 'manual', False, "执行模式（固定为manual）")
        self.set_default('strategy2b', 'position_size', 8, True, "默认开仓金额（USDT）")
        self.set_default('strategy2b', 'min_basis', 0.02, True, "最小基差（筛选阈值）")

        # 策略3：单边资金费率趋势策略
        self.set_default('strategy3', 'enabled', True, True, "是否启用")
        self.set_default('strategy3', 'min_funding_rate', 0.0001, True, "最小资金费率（0.01%）")
        self.set_default('strategy3', 'position_size', 10, True, "默认开仓金额（USDT）")
        self.set_default('strategy3', 'stop_loss_pct', 0.05, True, "止损比例（5%）")
        self.set_default('strategy3', 'check_basis', False, True, "是否检查基差方向（推荐关闭）")
        self.set_default('strategy3', 'short_exit_threshold', 0.0, True, "做空退出费率阈值")
        self.set_default('strategy3', 'long_exit_threshold', 0.0, True, "做多退出费率阈值")
        self.set_default('strategy3', 'trailing_stop_enabled', True, True, "是否启用动态追踪止盈")
        self.set_default('strategy3', 'trailing_activation_pct', 0.04, True, "追踪止盈启动阈值（4%盈利）")
        self.set_default('strategy3', 'trailing_callback_pct', 0.04, True, "追踪止盈回撤阈值（4%回撤）")

        # 风控配置
        self.set_default('risk', 'max_position_size_per_trade', 30, True, "单笔最大仓位（USDT）")
        self.set_default('risk', 'max_drawdown', 0.10, True, "总资金最大回撤")
        self.set_default('risk', 'warning_threshold', 0.05, True, "警告级别浮亏阈值（5%）")
        self.set_default('risk', 'critical_threshold', 0.10, True, "严重级别浮亏阈值（10%）")
        self.set_default('risk', 'emergency_threshold', 0.15, True, "紧急级别浮亏阈值（15%）")
        self.set_default('risk', 'price_deviation_threshold', 0.02, True, "价格偏离阈值")
        self.set_default('risk', 'abnormal_funding_rate', 0.005, True, "异常资金费率阈值")
        self.set_default('risk', 'min_depth_multiplier', 10, True, "最小深度倍数")

        # 动态仓位调整
        self.set_default('risk', 'dynamic_position_enabled', True, True, "启用动态仓位调整")
        self.set_default('risk', 'high_score_multiplier', 1.5, True, "高评分仓位倍数（>85分）")
        self.set_default('risk', 'medium_score_multiplier', 1.0, True, "中等评分仓位倍数（60-85分）")
        self.set_default('risk', 'low_score_multiplier', 0.5, True, "低评分仓位倍数（<60分）")

        logger.info("Default configurations initialized")
