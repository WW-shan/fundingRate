"""
配置管理 API（监控版）
"""
from flask import Blueprint, jsonify, request, current_app
from loguru import logger
from web.auth import api_auth_required

config_api_bp = Blueprint('config_api', __name__, url_prefix='/api')


@config_api_bp.route('/config')
@api_auth_required
def get_config():
    """获取所有配置"""
    cm = current_app.config['CONFIG_MANAGER']
    try:
        configs = {
            'global': {
                'enabled_exchanges': cm.get('global', 'enabled_exchanges',
                                            ['binance', 'okx', 'bybit', 'gate', 'bitget']),
                'price_refresh_interval': cm.get('global', 'price_refresh_interval', 30),
                'funding_refresh_interval': cm.get('global', 'funding_refresh_interval', 300),
                'stablecoin_refresh_interval': cm.get('global', 'stablecoin_refresh_interval', 60),
                'monitor_scan_interval': cm.get('global', 'monitor_scan_interval', 60),
                'stablecoin_symbols': cm.get('global', 'stablecoin_symbols',
                                             ['USDC/USDT', 'FDUSD/USDT', 'TUSD/USDT', 'DAI/USDT', 'USDE/USDT']),
            },
            'alerts': {
                'funding_rate_threshold': cm.get('alerts', 'funding_rate_threshold', 0.001),
                'funding_apr_threshold': cm.get('alerts', 'funding_apr_threshold', 0.30),
                'cross_spread_threshold': cm.get('alerts', 'cross_spread_threshold', 0.50),
                'basis_threshold': cm.get('alerts', 'basis_threshold', 0.02),
                'depeg_warning': cm.get('alerts', 'depeg_warning', 0.003),
                'depeg_critical': cm.get('alerts', 'depeg_critical', 0.01),
                'daily_summary_enabled': cm.get('alerts', 'daily_summary_enabled', True),
                'daily_summary_hour': cm.get('alerts', 'daily_summary_hour', 9),
                'push_funding_apr_threshold': cm.get('alerts', 'push_funding_apr_threshold', 10.0),
                'push_depeg': cm.get('alerts', 'push_depeg', True),
            },
        }
        return jsonify({'success': True, 'data': configs})
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({'success': False, 'error': str(e)})


@config_api_bp.route('/config/update', methods=['POST'])
@api_auth_required
def update_config():
    """更新配置"""
    cm = current_app.config['CONFIG_MANAGER']
    try:
        data = request.get_json()
        category = data.get('category')
        key = data.get('key')
        value = data.get('value')

        if not all([category, key, value is not None]):
            return jsonify({'success': False, 'error': '缺少必需字段'})

        cm.set(category, key, value, is_hot_reload=True)
        logger.info(f"Config updated: {category}.{key} = {value}")
        return jsonify({'success': True, 'message': '配置已更新'})
    except Exception as e:
        logger.error(f"Error updating config: {e}")
        return jsonify({'success': False, 'error': str(e)})


@config_api_bp.route('/config/reload', methods=['POST'])
@api_auth_required
def reload_config():
    """重载热更新配置"""
    cm = current_app.config['CONFIG_MANAGER']
    try:
        cm.reload_hot_configs()
        return jsonify({'success': True, 'message': '配置已重载'})
    except Exception as e:
        logger.error(f"Error reloading config: {e}")
        return jsonify({'success': False, 'error': str(e)})


@config_api_bp.route('/exchanges/reload', methods=['POST'])
@api_auth_required
def reload_exchanges():
    """重载交易所连接（切换代理/启用列表后使用）"""
    dc = current_app.config['DATA_COLLECTOR']
    try:
        dc.reload_exchanges()
        return jsonify({'success': True, 'message': '交易所连接已重载',
                        'active': list(dc.exchanges.keys())})
    except Exception as e:
        logger.error(f"Error reloading exchanges: {e}")
        return jsonify({'success': False, 'error': str(e)})
