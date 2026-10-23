"""
配置管理器
"""
import json
from typing import Any
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

        # 兼容通过 API/表单写入的大小写布尔字符串
        if isinstance(value, str):
            stripped = value.strip()
            lowered = stripped.lower()
            if lowered == 'true':
                return True
            if lowered == 'false':
                return False
            if lowered in ('none', 'null'):
                return None

        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    def get_int(self, category: str, key: str, default: int = 0) -> int:
        try:
            return int(self.get(category, key, default))
        except (TypeError, ValueError):
            return int(default)

    def get_float(self, category: str, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(category, key, default))
        except (TypeError, ValueError):
            return float(default)

    def get_bool(self, category: str, key: str, default: bool = False) -> bool:
        value = self.get(category, key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)

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

    def init_default_configs(self):
        """初始化默认配置"""
        # 全局配置
        self.set_default('global', 'price_refresh_interval', 30, True, "价格刷新间隔（秒）")
        self.set_default('global', 'funding_refresh_interval', 300, True, "资金费率刷新间隔（秒）")
        self.set_default('global', 'stablecoin_refresh_interval', 60, True, "稳定币价格刷新间隔（秒）")
        self.set_default('global', 'reference_refresh_interval', 300, True, "参考价刷新间隔（秒，黄金/欧元/商品）")
        self.set_default('global', 'monitor_scan_interval', 60, True, "异常扫描间隔（秒）")
        self.set_default('global', 'price_stale_threshold', 600, True, "行情陈旧阈值（秒），超过则丢弃")
        self.set_default('global', 'funding_min_volume_usd', 500000, True,
                         "资金费率/基差机会的最小 24h 永续成交额（USDT），低于此值不进入排行/告警")
        self.set_default('global', 'enabled_exchanges', ['binance', 'okx', 'bybit', 'gate', 'bitget'], True, "启用的交易所")

        # 告警阈值配置（脱锚阈值按资产类别在 peg_assets.THRESHOLDS 中定义）
        self.set_default('alerts', 'funding_apr_threshold', 10.0, True, "年化资金费率告警阈值（1000%）")
        self.set_default('alerts', 'funding_min_exchanges', 2, True, "资金费率异常至少多少家交易所同时触发才告警（默认 2）")
        self.set_default('alerts', 'cross_spread_threshold', 1.0, True, "跨所年化费率差告警阈值（100%）")
        self.set_default('alerts', 'basis_threshold', 0.05, True,
                         "基差告警阈值（5%，适用于所有现货/永续配对标的）")
        self.set_default('alerts', 'max_pushes_per_scan', 20, True, "每轮扫描最多推送条数（每条告警单独发送）")
        self.set_default('alerts', 'push_funding_apr_threshold', 10.0, True, "推送所需的年化费率阈值（1000%）")
        self.set_default('alerts', 'push_basis_threshold', 0.05, True,
                         "基差推送阈值（5%，适用于所有现货/永续配对标的）")
        self.set_default('alerts', 'push_reset_gap', 3600, True, "同一异常消失多久后视为恢复、再次出现时重新推送（秒）")
        self.set_default('alerts', 'push_reminder_cooldown', 86400, True, "同一档位持续存在时的最长提醒间隔（秒，默认24h）")
        self.set_default('alerts', 'push_depeg', True, True, "推送锚定资产偏离告警")
        self.set_default('alerts', 'push_cross_spread', False, True, "推送跨所费率差（默认关闭，仅面板）")
        self.set_default('alerts', 'push_basis', True, True, "推送期现基差告警")
        self.set_default('alerts', 'persist_cooldown', 1800, True, "同标的告警落库冷却（秒）")
        self.set_default('alerts', 'peg_min_volume_usd', 500000, True, "锚定资产流动性门槛（24h 成交额 USD）")
        self.set_default('alerts', 'issuer_spread_threshold', 0.01, True, "跨发行商价差告警阈值（1%）")
        self.set_default('alerts', 'push_issuer_spread', True, True, "跨发行商价差是否推送")
        self.set_default('alerts', 'daily_summary_enabled', True, True, "每日汇总推送开关")
        self.set_default('alerts', 'daily_summary_hour', 9, True, "每日汇总推送时间（小时，本地时间）")

        # 清理已废弃配置，避免面板/数据库出现误导项
        for category, key in (
            ('alerts', 'funding_rate_threshold'),
            ('alerts', 'depeg_warning'),
            ('alerts', 'depeg_critical'),
            ('global', 'stablecoin_symbols'),
        ):
            self.db.execute_query("DELETE FROM config WHERE category = ? AND key = ?", (category, key))
            self._config_cache.pop(f"{category}.{key}", None)

        logger.info("Default monitor configs initialized")
