"""
交易所管理API路由
"""
from flask import Blueprint, jsonify, request, current_app
from loguru import logger
from web.auth import api_auth_required

exchange_api_bp = Blueprint('exchange_api', __name__, url_prefix='/api/exchanges')


@exchange_api_bp.route('')
@api_auth_required
def list_exchanges():
    """获取交易所列表"""
    db_manager = current_app.config['DB_MANAGER']
    
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT exchange_name, is_active, created_at 
                FROM exchange_accounts 
                ORDER BY created_at DESC
            """)
            columns = [desc[0] for desc in cursor.description]
            exchanges = [dict(zip(columns, row)) for row in cursor.fetchall()]

        return jsonify({'success': True, 'data': exchanges})
    except Exception as e:
        logger.error(f"Error getting exchanges: {e}")
        return jsonify({'success': False, 'error': str(e)})


@exchange_api_bp.route('/add', methods=['POST'])
@api_auth_required
def add_exchange():
    """添加交易所账户"""
    data_collector = current_app.config['DATA_COLLECTOR']
    
    try:
        data = request.get_json()
        exchange_name = data.get('exchange_name', '').lower().strip()
        api_key = data.get('api_key', '').strip()
        api_secret = data.get('api_secret', '').strip()
        passphrase = data.get('passphrase', '').strip() or None

        # 验证
        if not all([exchange_name, api_key, api_secret]):
            return jsonify({'success': False, 'error': '请填写完整信息'})

        if exchange_name not in ['binance', 'okx', 'bybit', 'gate', 'bitget']:
            return jsonify({'success': False, 'error': '不支持的交易所'})

        # OKX 和 Bitget 必须有 passphrase
        if exchange_name in ['okx', 'bitget'] and not passphrase:
            return jsonify({'success': False, 'error': f'{exchange_name.upper()} 必须提供 Passphrase'})

        # 先创建交易所适配器并测试连接
        try:
            from exchanges import (
                BinanceAdapter, OKXAdapter, BybitAdapter,
                GateAdapter, BitgetAdapter
            )
            
            test_adapter = None
            if exchange_name == 'binance':
                test_adapter = BinanceAdapter(api_key, api_secret)
            elif exchange_name == 'okx':
                test_adapter = OKXAdapter(api_key, api_secret, passphrase)
            elif exchange_name == 'bybit':
                test_adapter = BybitAdapter(api_key, api_secret)
            elif exchange_name == 'gate':
                test_adapter = GateAdapter(api_key, api_secret)
            elif exchange_name == 'bitget':
                test_adapter = BitgetAdapter(api_key, api_secret, passphrase)
            
            # 测试连接
            if not test_adapter or not test_adapter.test_connection():
                return jsonify({
                    'success': False, 
                    'error': f'{exchange_name.upper()} 连接测试失败！请检查：\n1. API密钥是否正确\n2. API权限是否足够\n3. IP白名单设置（如有）\n4. 网络连接是否正常'
                })
                
        except Exception as conn_error:
            logger.error(f"Connection test failed for {exchange_name}: {conn_error}")
            return jsonify({
                'success': False, 
                'error': f'{exchange_name.upper()} 连接失败：{str(conn_error)}'
            })

        # 连接测试成功，保存账户信息
        if hasattr(data_collector, 'account_manager'):
            success = data_collector.account_manager.add_account(
                exchange_name, api_key, api_secret, passphrase
            )
            
            if not success:
                return jsonify({'success': False, 'error': '保存账户信息失败'})
            
            # 热更新交易所连接
            data_collector.reload_exchanges()
            
            logger.info(f"Exchange account added and connected: {exchange_name}")
            return jsonify({
                'success': True, 
                'message': f'✅ {exchange_name.upper()} 连接成功并已保存！'
            })
        else:
            return jsonify({'success': False, 'error': '系统未初始化账户管理器'})

    except Exception as e:
        logger.error(f"Error adding exchange: {e}")
        return jsonify({'success': False, 'error': f'添加失败：{str(e)}'})


@exchange_api_bp.route('/delete', methods=['POST'])
@api_auth_required
def delete_exchange():
    """删除交易所账户"""
    data_collector = current_app.config['DATA_COLLECTOR']
    
    try:
        data = request.get_json()
        exchange_name = data.get('exchange_name', '').lower().strip()

        if not exchange_name:
            return jsonify({'success': False, 'error': '请指定交易所'})

        # 使用account_manager删除账户
        if hasattr(data_collector, 'account_manager'):
            success = data_collector.account_manager.remove_account(exchange_name)
            
            if success:
                # 热更新交易所连接
                data_collector.reload_exchanges()
                logger.info(f"Exchange account deleted and reloaded: {exchange_name}")
                return jsonify({'success': True, 'message': f'{exchange_name.upper()} 已删除！'})
            else:
                return jsonify({'success': False, 'error': '删除失败'})
        else:
            return jsonify({'success': False, 'error': '系统未初始化账户管理器'})

    except Exception as e:
        logger.error(f"Error deleting exchange: {e}")
        return jsonify({'success': False, 'error': str(e)})


@exchange_api_bp.route('/status')
@api_auth_required
def exchange_status():
    """获取交易所数据采集状态"""
    data_collector = current_app.config['DATA_COLLECTOR']
    
    try:
        status_list = []
        
        if data_collector:
            # 获取所有连接的交易所
            connected_exchanges = list(data_collector.exchanges.keys())
            market_data = data_collector.market_data
            
            for exchange_name in connected_exchanges:
                # 统计该交易所的数据
                symbols_with_data = []
                for symbol, exchanges_data in market_data.items():
                    if exchange_name in exchanges_data:
                        ex_data = exchanges_data[exchange_name]
                        # 检查是否有实际数据
                        if ex_data.get('spot_price') or ex_data.get('futures_price'):
                            symbols_with_data.append(symbol)
                
                status_list.append({
                    'exchange': exchange_name,
                    'connected': True,
                    'symbols_count': len(symbols_with_data),
                    'symbols': symbols_with_data,
                    'last_update': market_data.get(symbols_with_data[0], {}).get(exchange_name, {}).get('timestamp') if symbols_with_data else None
                })
        
        return jsonify({
            'success': True,
            'data': {
                'exchanges': status_list,
                'total_symbols': len(data_collector.market_data) if data_collector else 0,
                'collector_running': data_collector.running if data_collector else False
            }
        })
    except Exception as e:
        logger.error(f"Error getting exchange status: {e}")
        return jsonify({'success': False, 'error': str(e)})
