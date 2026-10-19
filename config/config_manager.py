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
        """交易对默认配置（监控版：仅监控开关）"""
        return {
            'symbol': symbol,
            'exchange': exchange,
            'is_active': True,
        }

    def init_default_configs(self):
        """初始化默认配置"""
        # 全局配置
        self.set_default('global', 'price_refresh_interval', 30, True, "价格刷新间隔（秒）")
        self.set_default('global', 'funding_refresh_interval', 300, True, "资金费率刷新间隔（秒）")
        self.set_default('global', 'stablecoin_refresh_interval', 60, True, "稳定币价格刷新间隔（秒）")
        self.set_default('global', 'monitor_scan_interval', 60, True, "异常扫描间隔（秒）")
        self.set_default('global', 'price_stale_threshold', 600, True, "行情陈旧阈值（秒），超过则丢弃")
        self.set_default('global', 'enabled_exchanges', ['binance', 'okx', 'bybit', 'gate', 'bitget'], True, "启用的交易所")
        self.set_default('global', 'stablecoin_symbols',
                         ['USDC/USDT', 'FDUSD/USDT', 'TUSD/USDT', 'DAI/USDT', 'USDE/USDT'],
                         True, "监控的稳定币交易对")

        # 告警阈值配置
        self.set_default('alerts', 'funding_rate_threshold', 0.003, True, "单期资金费率告警阈值（0.3%）")
        self.set_default('alerts', 'funding_apr_threshold', 1.0, True, "年化资金费率告警阈值（100%）")
        self.set_default('alerts', 'cross_spread_threshold', 1.0, True, "跨所年化费率差告警阈值（100%）")
        self.set_default('alerts', 'basis_threshold', 0.02, True, "基差告警阈值（2%）")
        self.set_default('alerts', 'depeg_warning', 0.003, True, "稳定币脱锚预警阈值（0.3%）")
        self.set_default('alerts', 'depeg_critical', 0.01, True, "稳定币脱锚严重阈值（1%）")
        self.set_default('alerts', 'max_pushes_per_scan', 10, True, "每轮扫描最多推送告警数（防刷屏）")
        self.set_default('alerts', 'daily_summary_enabled', True, True, "每日汇总推送开关")
        self.set_default('alerts', 'daily_summary_hour', 9, True, "每日汇总推送时间（小时，本地时间）")

        logger.info("Default monitor configs initialized")
