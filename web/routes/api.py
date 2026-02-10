"""
API路由
"""
import os
from flask import Blueprint, jsonify, request, current_app
from loguru import logger
from web.auth import api_auth_required
import time

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/status')
def status():
    """系统状态API"""
    data_collector = current_app.config['DATA_COLLECTOR']
    return jsonify({
        'status': 'running',
        'trading_enabled': os.getenv('ENABLE_TRADING', 'False').lower() == 'true',
        'exchanges_connected': len(data_collector.exchanges) if data_collector else 0,
        'market_data_symbols': len(data_collector.market_data) if data_collector else 0
    })


@api_bp.route('/health')
def health():
    """健康检查API"""
    db_manager = current_app.config['DB_MANAGER']
    data_collector = current_app.config['DATA_COLLECTOR']
    opportunity_monitor = current_app.config['OPPORTUNITY_MONITOR']
    strategy_executor = current_app.config['STRATEGY_EXECUTOR']

    try:
        with db_manager.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()

        components = {
            'database': True,
            'data_collector': data_collector is not None,
            'opportunity_monitor': opportunity_monitor is not None,
            'strategy_executor': strategy_executor is not None
        }

        all_healthy = all(components.values())

        return jsonify({
            'status': 'healthy' if all_healthy else 'degraded',
            'components': components,
            'timestamp': time.time()
        }), 200 if all_healthy else 503

    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 503


@api_bp.route('/errors')
@api_auth_required
def errors():
    """获取最近错误"""
    try:
        return jsonify({'success': True, 'data': {'errors': [], 'total': 0}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/opportunities')
@api_auth_required
def opportunities():
    """当前机会API"""
    opportunity_monitor = current_app.config['OPPORTUNITY_MONITOR']
    db_manager = current_app.config['DB_MANAGER']
    
    try:
        # 获取最新机会
        opportunities = opportunity_monitor.get_opportunities(limit=20) if opportunity_monitor else []
        
        # 获取所有开仓的持仓信息（用于标记哪些机会已开仓）
        open_positions = {}
        if db_manager:
            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, symbol, strategy_type, exchanges, entry_details
                    FROM positions
                    WHERE status = 'open'
                """)
                import json
                for row in cursor.fetchall():
                    pos_id, symbol, strategy_type, exchanges, entry_details = row
                    entry_details_dict = json.loads(entry_details) if entry_details else {}
                    
                    # 生成与机会ID相同的key用于匹配
                    if strategy_type == 'funding_rate_cross_exchange':
                        long_ex = entry_details_dict.get('long_exchange', '')
                        short_ex = entry_details_dict.get('short_exchange', '')
                        key = f"s1_{symbol}_{long_ex}_{short_ex}"
                    elif strategy_type == 'funding_rate_spot_futures':
                        key = f"s2a_{symbol}_{exchanges}"
                    elif strategy_type == 'basis_arbitrage':
                        key = f"s2b_{symbol}_{exchanges}"
                    else:
                        continue
                    open_positions[key] = pos_id
        
        # 为每个机会添加开仓状态
        for opp in opportunities:
            opp['has_open_position'] = opp['id'] in open_positions
            opp['position_id'] = open_positions.get(opp['id'])
        
        return jsonify({
            'success': True,
            'data': opportunities
        })
    except Exception as e:
        logger.error(f"Error getting opportunities: {e}")
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/positions')
@api_auth_required
def positions():
    """持仓API"""
    db_manager = current_app.config['DB_MANAGER']
    data_collector = current_app.config.get('DATA_COLLECTOR')
    
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, symbol, strategy_type, exchanges, position_size, entry_details,
                       current_pnl, realized_pnl, funding_collected,
                       fees_paid, status, open_time, close_time,
                       trailing_stop_activated, best_price, activation_price, entry_price
                FROM positions
                WHERE status IN ('open', 'emergency_close_pending')
                ORDER BY open_time DESC
                LIMIT 100
            """)
            columns = [desc[0] for desc in cursor.description]
            positions = [dict(zip(columns, row)) for row in cursor.fetchall()]
            
            # 确保所有数值字段都有默认值（处理NULL）
            for pos in positions:
                pos['position_size'] = float(pos.get('position_size') or 0)
                pos['current_pnl'] = float(pos.get('current_pnl') or 0)
                pos['realized_pnl'] = float(pos.get('realized_pnl') or 0)
                pos['funding_collected'] = float(pos.get('funding_collected') or 0)
                pos['fees_paid'] = float(pos.get('fees_paid') or 0)
            
            # 为每个持仓添加实时市场数据和准确计算
            if data_collector:
                import json
                for pos in positions:
                    symbol = pos['symbol']
                    exchanges = pos['exchanges']
                    entry_details = json.loads(pos['entry_details']) if pos['entry_details'] else {}
                    position_size = float(pos.get('position_size', 0))
                    
                    # 从entry_details提取entry_price
                    entry_price = 0
                    if pos['strategy_type'] == 'funding_rate_cross_exchange':
                        # 策略1: 使用long_price和short_price的平均值
                        long_price = float(entry_details.get('long_price', 0))
                        short_price = float(entry_details.get('short_price', 0))
                        entry_price = (long_price + short_price) / 2 if (long_price + short_price) > 0 else 0
                    elif pos['strategy_type'] == 'funding_rate_spot_futures':
                        # 策略2A: 使用spot_price
                        entry_price = float(entry_details.get('spot_price', 0))
                    elif pos['strategy_type'] == 'basis_arbitrage':
                        # 策略2B: 使用spot_price
                        entry_price = float(entry_details.get('spot_price', 0))
                    else:
                        # 其他策略：尝试从entry_details获取
                        entry_price = float(entry_details.get('entry_price', 0))
                    
                    pos['entry_price'] = entry_price
                    
                    # 获取实时市场数据
                    market_data = data_collector.get_market_data(symbol)
                    
                    if market_data and exchanges in market_data:
                        exchange_data = market_data[exchanges]
                        pos['current_spot_price'] = exchange_data.get('spot_price')
                        pos['current_futures_price'] = exchange_data.get('futures_price')
                        pos['current_spot_ask'] = exchange_data.get('spot_ask')
                        pos['current_spot_bid'] = exchange_data.get('spot_bid')
                        pos['current_futures_bid'] = exchange_data.get('futures_bid')
                        pos['current_futures_ask'] = exchange_data.get('futures_ask')
                        pos['current_funding_rate'] = exchange_data.get('funding_rate')
                        pos['next_funding_time'] = exchange_data.get('next_funding_time')
                        
                        # 计算当前基差（如果适用）
                        if exchange_data.get('spot_ask') and exchange_data.get('futures_bid'):
                            pos['current_basis'] = (exchange_data['futures_bid'] - exchange_data['spot_ask']) / exchange_data['spot_ask']
                        
                        # 计算基差变化（策略2A/2B）
                        if 'basis' in entry_details and pos.get('current_basis') is not None:
                            pos['basis_change'] = pos['current_basis'] - entry_details['basis']
                        
                        # 计算价格变化百分比
                        if entry_price > 0:
                            if pos['strategy_type'] == 'funding_rate_spot_futures':
                                # 策略2A：计算现货和期货价格变化
                                spot_entry = float(entry_details.get('spot_price', entry_price))
                                futures_entry = float(entry_details.get('futures_price', entry_price))
                                if spot_entry > 0 and exchange_data.get('spot_price'):
                                    pos['spot_price_change_pct'] = ((exchange_data['spot_price'] - spot_entry) / spot_entry) * 100
                                if futures_entry > 0 and exchange_data.get('futures_price'):
                                    pos['futures_price_change_pct'] = ((exchange_data['futures_price'] - futures_entry) / futures_entry) * 100
                            else:
                                # 其他策略：计算总体价格变化
                                current_price = exchange_data.get('futures_price') or exchange_data.get('spot_price')
                                if current_price:
                                    pos['price_change_pct'] = ((current_price - entry_price) / entry_price) * 100
                        
                        # 计算实时盈亏率
                        if position_size > 0:
                            pos['pnl_percentage'] = (float(pos.get('current_pnl', 0)) / position_size) * 100
                        else:
                            pos['pnl_percentage'] = 0

        return jsonify({'success': True, 'data': positions})
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/close_position/<int:position_id>', methods=['POST'])
@api_auth_required
def close_position(position_id):
    """平仓API"""
    strategy_executor = current_app.config['STRATEGY_EXECUTOR']
    
    try:
        if not strategy_executor:
            return jsonify({'success': False, 'error': '策略执行器未初始化'})

        result = strategy_executor.close_position(position_id)
        if result:
            return jsonify({'success': True, 'message': '平仓成功'})
        else:
            return jsonify({'success': False, 'error': '平仓失败'})
    except Exception as e:
        logger.error(f"Error closing position: {e}")
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/execute_opportunity', methods=['POST'])
@api_auth_required
def execute_opportunity():
    """执行套利机会API"""
    strategy_executor = current_app.config['STRATEGY_EXECUTOR']
    
    try:
        if not strategy_executor:
            return jsonify({'success': False, 'error': '策略执行器未初始化'})
        
        data = request.get_json()
        opportunity_id = data.get('opportunity_id')
        opportunity_data = data.get('opportunity')  # 前端传递的完整机会数据
        
        if not opportunity_id:
            return jsonify({'success': False, 'error': '缺少opportunity_id'})
        
        # 优先使用前端传递的机会数据（避免已过期问题）
        opportunity = opportunity_data
        
        # 如果前端没有传递，则从监控器查找
        if not opportunity:
            opportunity_monitor = current_app.config['OPPORTUNITY_MONITOR']
            if not opportunity_monitor:
                return jsonify({'success': False, 'error': '机会监控器未初始化'})
            
            # 从机会列表中查找
            for opp in opportunity_monitor.opportunities:
                if opp.get('id') == opportunity_id:
                    opportunity = opp
                    break
        
        if not opportunity:
            return jsonify({'success': False, 'error': '机会不存在或已过期'})
        
        # 执行套利
        result = strategy_executor.execute_opportunity(opportunity)
        
        if result.get('success'):
            return jsonify({
                'success': True, 
                'message': '开仓成功！',
                'position_id': result.get('position_id')
            })
        else:
            return jsonify({
                'success': False, 
                'error': result.get('error', '开仓失败')
            })
            
    except Exception as e:
        logger.error(f"Error executing opportunity: {e}")
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/account_info')
@api_auth_required
def account_info():
    """获取所有交易所的账户信息"""
    data_collector = current_app.config['DATA_COLLECTOR']
    
    try:
        if not data_collector or not data_collector.exchanges:
            return jsonify({'success': False, 'error': '没有连接的交易所'})
        
        accounts_info = []
        total_usdt_all = 0
        
        for exchange_name, exchange_adapter in data_collector.exchanges.items():
            try:
                # 获取账户信息
                info = exchange_adapter.get_account_info()
                
                # 提取主要币种余额（只显示价值较大的）
                main_balances = {}
                for currency, balance_info in info['balances'].items():
                    if balance_info['total'] > 0.01:  # 过滤掉太小的余额
                        main_balances[currency] = balance_info
                
                account_data = {
                    'exchange': exchange_name,
                    'total_usdt': info['total_usdt'],
                    'positions_count': info['positions_count'],
                    'balances': main_balances,
                    'timestamp': info['timestamp']
                }
                
                accounts_info.append(account_data)
                total_usdt_all += info['total_usdt']
                
            except Exception as ex:
                logger.error(f"Error getting account info for {exchange_name}: {ex}")
                accounts_info.append({
                    'exchange': exchange_name,
                    'total_usdt': 0,
                    'positions_count': 0,
                    'balances': {},
                    'error': str(ex)
                })
        
        return jsonify({
            'success': True,
            'data': {
                'accounts': accounts_info,
                'total_usdt': round(total_usdt_all, 2)
            }
        })
        
    except Exception as e:
        logger.error(f"Error getting account info: {e}")
        return jsonify({'success': False, 'error': str(e)})

