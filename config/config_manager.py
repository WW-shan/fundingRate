"""
配置管理器
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

        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    def set(self, category: str, key: str, value: Any,
            is_hot_reload: bool = True, description: str = ""):
        """设置配置值"""
        if isinstance(value, (dict, list, bool, int, float, type(None))):
            value_str = json.dumps(value)
        else:
            value_str = str(value)

        self.db.set_config(category, key, value_str, is_hot_reload, description)

        cache_key = f"{category}.{key}"
        self._config_cache[cache_key] = value_str

    def set_default(self, category: str, key: str, value: Any,
                   is_hot_reload: bool = True, description: str = ""):
        """设置默认配置值（不覆盖已有配置）"""
        cache_key = f"{category}.{key}"
        if cache_key in self._config_cache:
            return
        self.set(category, key, value, is_hot_reload, description)

    def reload_hot_configs(self):
        """重新加载支持热更新的配置"""
        configs = self.db.execute_query(
            "SELECT * FROM config WHERE is_hot_reload = TRUE"
        )
        for cfg in configs:
            key = f"{cfg['category']}.{cfg['key']}"
            self._config_cache[key] = cfg['value']

    def get_pair_config(self, symbol: str, exchange: Optional[str] = None,
                       strategy_prefix: Optional[str] = None) -> Dict[str, Any]:
        """获取交易对配置"""
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
            return pair_configs[0]
        return self._get_default_pair_config(symbol, exchange)

    def _get_default_pair_config(self, symbol: str, exchange: Optional[str]) -> Dict[str, Any]:
        """获取交易对的默认配置"""
        return {
            'symbol': symbol,
            'exchange': exchange,
            's3_enabled': self.get('strategy3', 'enabled', True),
            's3_min_funding_rate': self.get('strategy3', 'min_funding_rate', 0.0001),
            's3_position_size': self.get('strategy3', 'position_size', 10),
            's3_short_exit_threshold': self.get('strategy3', 'short_exit_threshold', 0.0),
            's3_long_exit_threshold': self.get('strategy3', 'long_exit_threshold', 0.0),
            's3_trailing_stop_enabled': self.get('strategy3', 'trailing_stop_enabled', True),
            's3_trailing_activation_pct': self.get('strategy3', 'trailing_activation_pct', 0.04),
            's3_trailing_callback_pct': self.get('strategy3', 'trailing_callback_pct', 0.04),
            'max_positions': 3,
            'is_active': True
        }

    def init_default_configs(self):
        """初始化默认配置"""
        # 全局配置
        self.set_default('global', 'total_capital', 100, True, "总资金池（USDT）")
        self.set_default('global', 'max_capital_usage', 0.8, True, "最大资金使用率")
        self.set_default('global', 'max_positions', 10, True, "最大同时持仓数")
        self.set_default('global', 'price_refresh_interval', 5, True, "价格刷新间隔（秒）")
        self.set_default('global', 'funding_refresh_interval', 300, True, "资金费率刷新间隔（秒）")
        self.set_default('global', 'opportunity_scan_interval', 10, True, "机会扫描间隔（秒）")

        # 策略3：单边资金费率趋势策略
        self.set_default('strategy3', 'enabled', True, True, "是否启用")
        self.set_default('strategy3', 'min_funding_rate', 0.0001, True, "最小资金费率（0.01%）")
        self.set_default('strategy3', 'position_size', 10, True, "默认开仓金额（USDT）")
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

        logger.info("Default configurations initialized")
