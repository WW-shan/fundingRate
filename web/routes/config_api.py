"""
配置管理API路由
"""
from flask import Blueprint, jsonify, request, current_app
from loguru import logger
from web.auth import api_auth_required

config_api_bp = Blueprint('config_api', __name__, url_prefix='/api')


@config_api_bp.route('/config')
@api_auth_required
def get_config():
    """获取所有配置API"""
    config_manager = current_app.config['CONFIG_MANAGER']
    
    try:
        configs = {}
        if config_manager:
            # 全局配置
            configs['global'] = {
                'total_capital': config_manager.get('global', 'total_capital', 100000),
                'max_capital_usage': config_manager.get('global', 'max_capital_usage', 0.8),
                'max_positions': config_manager.get('global', 'max_positions', 10),
                'price_refresh_interval': config_manager.get('global', 'price_refresh_interval', 5),
                'funding_refresh_interval': config_manager.get('global', 'funding_refresh_interval', 300),
                'opportunity_scan_interval': config_manager.get('global', 'opportunity_scan_interval', 10),
            }
            
            # 策略1配置
            configs['strategy1'] = {
                'enabled': config_manager.get('strategy1', 'enabled', True),
                'execution_mode': config_manager.get('strategy1', 'execution_mode', 'auto'),
                'position_size': config_manager.get('strategy1', 'position_size', 10),
                'min_funding_diff': config_manager.get('strategy1', 'min_funding_diff', 0.0005),
                'max_price_diff': config_manager.get('strategy1', 'max_price_diff', 0.02),
                'max_position_size': config_manager.get('strategy1', 'max_position_size', 15),
            }
            
            # 策略2A配置
            configs['strategy2a'] = {
                'enabled': config_manager.get('strategy2a', 'enabled', True),
                'execution_mode': config_manager.get('strategy2a', 'execution_mode', 'auto'),
                'position_size': config_manager.get('strategy2a', 'position_size', 10),
                'min_funding_rate': config_manager.get('strategy2a', 'min_funding_rate', 0.0005),
                'max_basis_deviation': config_manager.get('strategy2a', 'max_basis_deviation', 0.01),
                'max_position_size': config_manager.get('strategy2a', 'max_position_size', 15),
            }
            
            # 策略2B配置
            configs['strategy2b'] = {
                'enabled': config_manager.get('strategy2b', 'enabled', True),
                'execution_mode': config_manager.get('strategy2b', 'execution_mode', 'manual'),
                'position_size': config_manager.get('strategy2b', 'position_size', 8),
                'min_basis': config_manager.get('strategy2b', 'min_basis', 0.02),
            }

            # 策略3配置
            configs['strategy3'] = {
                'enabled': config_manager.get('strategy3', 'enabled', False),
                'min_funding_rate': config_manager.get('strategy3', 'min_funding_rate', 0.0001),
                'position_size': config_manager.get('strategy3', 'position_size', 10),
                'stop_loss_pct': config_manager.get('strategy3', 'stop_loss_pct', 0.05),
                'check_basis': config_manager.get('strategy3', 'check_basis', True),
                'short_exit_threshold': config_manager.get('strategy3', 'short_exit_threshold', 0.0),
                'long_exit_threshold': config_manager.get('strategy3', 'long_exit_threshold', 0.0),
            }

            # 风控配置
            configs['risk'] = {
                'max_position_size_per_trade': config_manager.get('risk', 'max_position_size_per_trade', 30),
                'max_drawdown': config_manager.get('risk', 'max_drawdown', 0.10),
                'dynamic_position_enabled': config_manager.get('risk', 'dynamic_position_enabled', True),
                'warning_threshold': config_manager.get('risk', 'warning_threshold', 0.005),
                'critical_threshold': config_manager.get('risk', 'critical_threshold', 0.01),
                'emergency_threshold': config_manager.get('risk', 'emergency_threshold', 0.015),
            }
            
        return jsonify({'success': True, 'data': configs})
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({'success': False, 'error': str(e)})


@config_api_bp.route('/config/update', methods=['POST'])
@api_auth_required
def update_config():
    """更新配置API"""
    config_manager = current_app.config['CONFIG_MANAGER']
    
    try:
        data = request.get_json()
        category = data.get('category')
        key = data.get('key')
        value = data.get('value')

        if not all([category, key, value is not None]):
            return jsonify({'success': False, 'error': '缺少必需字段'})

        # 添加重试逻辑
        max_retries = 3
        import time
        import sqlite3

        for attempt in range(max_retries):
            try:
                # 正确调用ConfigManager的set方法（category和key是分开的参数）
                config_manager.set(category, key, value, is_hot_reload=True)
                logger.info(f"Config updated via web: {category}.{key} = {value}")
                return jsonify({'success': True, 'message': '配置已更新'})
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                raise e
            except Exception as e:
                raise e

    except Exception as e:
        logger.error(f"Error updating config: {e}")
        return jsonify({'success': False, 'error': str(e)})
