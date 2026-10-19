"""
API 路由（监控版）
"""
import time
from flask import Blueprint, jsonify, request, current_app
from loguru import logger
from web.auth import api_auth_required

api_bp = Blueprint('api', __name__, url_prefix='/api')


@api_bp.route('/status')
def status():
    """系统状态"""
    data_collector = current_app.config['DATA_COLLECTOR']
    market_monitor = current_app.config['MARKET_MONITOR']
    alert_manager = current_app.config['ALERT_MANAGER']

    dc_status = data_collector.get_status() if data_collector else {}
    stats = alert_manager.get_alert_stats(24) if alert_manager else {}
    scan = market_monitor.last_scan if market_monitor else {}

    return jsonify({
        'status': 'running',
        'mode': 'monitor',
        'exchanges': dc_status.get('exchanges', []),
        'symbols_tracked': dc_status.get('symbols_tracked', 0),
        'stablecoins_tracked': dc_status.get('stablecoins_tracked', 0),
        'alerts_24h': stats.get('total', 0),
        'last_scan_ts': scan.get('timestamp'),
    })


@api_bp.route('/health')
def health():
    """健康检查"""
    db_manager = current_app.config['DB_MANAGER']
    data_collector = current_app.config['DATA_COLLECTOR']
    market_monitor = current_app.config['MARKET_MONITOR']
    try:
        with db_manager.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        components = {
            'database': True,
            'data_collector': data_collector is not None and bool(data_collector.exchanges),
            'market_monitor': market_monitor is not None,
        }
        all_healthy = all(components.values())
        return jsonify({
            'status': 'healthy' if all_healthy else 'degraded',
            'components': components,
            'timestamp': time.time(),
        }), 200 if all_healthy else 503
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 503


@api_bp.route('/summary')
@api_auth_required
def summary():
    """总览数据（仪表盘用）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    alert_manager = current_app.config['ALERT_MANAGER']
    scan = market_monitor.last_scan or {}
    return jsonify({
        'success': True,
        'data': {
            'timestamp': scan.get('timestamp'),
            'top_apr': scan.get('apr_ranking', [])[:10],
            'top_spreads': scan.get('cross_spreads', [])[:10],
            'top_basis': scan.get('basis', [])[:10],
            'depeg': scan.get('depeg', [])[:10],
            'alert_stats': alert_manager.get_alert_stats(24),
        }
    })


@api_bp.route('/funding/apr')
@api_auth_required
def funding_apr():
    """APR 排行（支持 ?limit= 和 ?exchange= 和 ?symbol= 过滤）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    limit = min(int(request.args.get('limit', 100)), 1000)
    exchange = request.args.get('exchange', '').lower()
    symbol = request.args.get('symbol', '').upper()

    ranking = market_monitor.last_scan.get('apr_ranking', [])
    if exchange:
        ranking = [r for r in ranking if r['exchange'] == exchange]
    if symbol:
        ranking = [r for r in ranking if symbol in r['symbol']]

    return jsonify({'success': True, 'data': ranking[:limit], 'total': len(ranking)})


@api_bp.route('/funding/spreads')
@api_auth_required
def funding_spreads():
    """跨所费率差"""
    market_monitor = current_app.config['MARKET_MONITOR']
    limit = min(int(request.args.get('limit', 100)), 1000)
    rows = market_monitor.last_scan.get('cross_spreads', [])
    return jsonify({'success': True, 'data': rows[:limit], 'total': len(rows)})


@api_bp.route('/basis')
@api_auth_required
def basis():
    """基差列表"""
    market_monitor = current_app.config['MARKET_MONITOR']
    limit = min(int(request.args.get('limit', 100)), 1000)
    rows = market_monitor.last_scan.get('basis', [])
    return jsonify({'success': True, 'data': rows[:limit], 'total': len(rows)})


@api_bp.route('/stablecoins')
@api_auth_required
def stablecoins():
    """稳定币锚定状态"""
    market_monitor = current_app.config['MARKET_MONITOR']
    rows = market_monitor.last_scan.get('depeg', [])
    return jsonify({'success': True, 'data': rows})


@api_bp.route('/stablecoins/history')
@api_auth_required
def stablecoin_history():
    """稳定币历史价格（?symbol=USDC/USDT&hours=24）"""
    db_manager = current_app.config['DB_MANAGER']
    symbol = request.args.get('symbol', 'USDC/USDT')
    hours = min(int(request.args.get('hours', 24)), 24 * 30)
    since = int((time.time() - hours * 3600) * 1000)
    try:
        rows = db_manager.execute_query(
            """
            SELECT exchange, symbol, timestamp, price, bid, ask
            FROM stablecoin_prices
            WHERE symbol = ? AND timestamp > ?
            ORDER BY timestamp ASC
            """,
            (symbol, since)
        )
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        logger.error(f"Stablecoin history failed: {e}")
        return jsonify({'success': False, 'error': str(e)})


@api_bp.route('/alerts')
@api_auth_required
def alerts():
    """告警记录（?hours=24&limit=100&level=critical&type=stablecoin_depeg）"""
    alert_manager = current_app.config['ALERT_MANAGER']
    hours = min(int(request.args.get('hours', 24)), 24 * 30)
    limit = min(int(request.args.get('limit', 100)), 1000)
    level = request.args.get('level', '')
    alert_type = request.args.get('type', '')

    rows = alert_manager.get_recent_alerts(limit=limit, hours=hours)
    if level:
        rows = [r for r in rows if r['level'] == level]
    if alert_type:
        rows = [r for r in rows if r['alert_type'] == alert_type]

    return jsonify({'success': True, 'data': rows, 'total': len(rows)})


@api_bp.route('/alerts/stats')
@api_auth_required
def alert_stats():
    """告警统计"""
    alert_manager = current_app.config['ALERT_MANAGER']
    hours = min(int(request.args.get('hours', 24)), 24 * 30)
    return jsonify({'success': True, 'data': alert_manager.get_alert_stats(hours)})


@api_bp.route('/funding/history')
@api_auth_required
def funding_history():
    """资金费率历史（?exchange=binance&symbol=BTC/USDT&hours=24）"""
    db_manager = current_app.config['DB_MANAGER']
    exchange = request.args.get('exchange', '')
    symbol = request.args.get('symbol', '')
    hours = min(int(request.args.get('hours', 24)), 24 * 30)
    since = int((time.time() - hours * 3600) * 1000)
    try:
        rows = db_manager.execute_query(
            """
            SELECT exchange, symbol, timestamp, funding_rate, next_funding_time, funding_interval
            FROM funding_rates
            WHERE exchange = ? AND symbol = ? AND timestamp > ?
            ORDER BY timestamp ASC
            """,
            (exchange, symbol, since)
        )
        return jsonify({'success': True, 'data': rows})
    except Exception as e:
        logger.error(f"Funding history failed: {e}")
        return jsonify({'success': False, 'error': str(e)})
