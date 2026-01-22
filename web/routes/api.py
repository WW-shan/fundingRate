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
    from utils.performance import performance_monitor
    
    db_manager = current_app.config['DB_MANAGER']
    data_collector = current_app.config['DATA_COLLECTOR']
    opportunity_monitor = current_app.config['OPPORTUNITY_MONITOR']
    strategy_executor = current_app.config['STRATEGY_EXECUTOR']
    
    try:
        # 检查数据库连接
        with db_manager.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()

        # 获取系统统计
        stats = performance_monitor.get_system_stats()

        # 检查关键组件
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
            'system_stats': stats,
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
    from utils.performance import error_collector
    
    try:
        summary = error_collector.get_error_summary()
        return jsonify({'success': True, 'data': summary})
    except Exception as e:
        logger.error(f"Error getting error summary: {e}")
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/opportunities')
@api_auth_required
def opportunities():
    """当前机会API"""
    opportunity_monitor = current_app.config['OPPORTUNITY_MONITOR']
    
    try:
        # 获取最新机会
        opportunities = opportunity_monitor.get_opportunities(limit=20) if opportunity_monitor else []
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
    
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, symbol, strategy_type, position_size,
                       current_pnl, realized_pnl, funding_collected,
                       fees_paid, status, open_time, close_time
                FROM positions
                WHERE status = 'open'
                ORDER BY open_time DESC
                LIMIT 50
            """)
            columns = [desc[0] for desc in cursor.description]
            positions = [dict(zip(columns, row)) for row in cursor.fetchall()]

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
