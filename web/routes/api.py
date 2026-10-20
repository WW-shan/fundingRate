"""
API 路由（监控版）
"""
import time
from flask import Blueprint, jsonify, request, current_app
from loguru import logger
from web.auth import api_auth_required

api_bp = Blueprint('api', __name__, url_prefix='/api')


def _int_arg(name: str, default: int, maximum: int) -> int:
    """安全解析整数查询参数，非法值回退默认值"""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(0, min(value, maximum))


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
        'status': 'running' if dc_status.get('running') else 'stopped',
        'mode': 'monitor',
        'exchanges': dc_status.get('exchanges', []),
        'symbols_tracked': dc_status.get('symbols_tracked', 0),
        'stablecoins_tracked': dc_status.get('stablecoins_tracked', 0),
        'peg_assets': dc_status.get('peg_assets', 0),
        'reference_prices': dc_status.get('reference_prices', 0),
        'alerts_24h': stats.get('total', 0),
        'pushed_24h': stats.get('pushed_total', 0),
        'last_scan_ts': scan.get('timestamp'),
    })


@api_bp.route('/health')
def health():
    """健康检查"""
    db_manager = current_app.config['DB_MANAGER']
    config_manager = current_app.config['CONFIG_MANAGER']
    data_collector = current_app.config['DATA_COLLECTOR']
    market_monitor = current_app.config['MARKET_MONITOR']
    try:
        with db_manager.get_connection() as conn:
            conn.execute("SELECT 1").fetchone()
        expected = config_manager.get(
            'global', 'enabled_exchanges',
            ['binance', 'okx', 'bybit', 'gate', 'bitget']
        )
        if isinstance(expected, str):
            expected = [x.strip().lower() for x in expected.split(',') if x.strip()]
        active = set(data_collector.exchanges) if data_collector else set()
        missing = sorted(set(expected) - active)
        scan = (market_monitor.last_scan or {}) if market_monitor else {}
        scan_ts = scan.get('timestamp')
        scan_age = (time.time() * 1000 - scan_ts) / 1000 if scan_ts else None
        scan_interval = config_manager.get_int('global', 'monitor_scan_interval', 60)
        scan_fresh = scan_age is not None and scan_age <= max(300, scan_interval * 3)
        components = {
            'database': True,
            'data_collector': data_collector is not None and bool(active),
            'all_enabled_exchanges': not missing,
            'market_monitor': market_monitor is not None and scan_fresh,
        }
        all_healthy = all(components.values())
        return jsonify({
            'status': 'healthy' if all_healthy else 'degraded',
            'components': components,
            'exchanges': sorted(active),
            'missing_exchanges': missing,
            'last_scan_age_seconds': round(scan_age, 1) if scan_age is not None else None,
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
    ranking = scan.get('apr_ranking', [])
    apr_values = [r['apr'] for r in ranking if r.get('apr') is not None]
    return jsonify({
        'success': True,
        'data': {
            'timestamp': scan.get('timestamp'),
            'max_abs_apr': max(apr_values, key=abs, default=None),
            'max_positive_apr': max(apr_values, default=None),
            'max_negative_apr': min(apr_values, default=None),
            'top_apr': ranking[:10],
            'top_spreads': scan.get('cross_spreads', [])[:10],
            'top_basis': scan.get('basis', [])[:10],
            'depeg': scan.get('depeg', [])[:10],
            'issuer_spreads': scan.get('issuer_spreads', [])[:10],
            'alert_stats': alert_manager.get_alert_stats(24),
        }
    })


@api_bp.route('/funding/apr')
@api_auth_required
def funding_apr():
    """APR 排行（支持 ?limit= 和 ?exchange= 和 ?symbol= 过滤）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    limit = _int_arg('limit', 1000, 10000)
    exchange = request.args.get('exchange', '').lower()
    symbol = request.args.get('symbol', '').upper()
    sort_key = request.args.get('sort', 'abs_desc')

    ranking = market_monitor.last_scan.get('apr_ranking', [])
    if exchange:
        ranking = [r for r in ranking if r['exchange'] == exchange]
    if symbol:
        exact = [r for r in ranking if r['symbol'].split('/')[0] == symbol]
        ranking = exact or [r for r in ranking if symbol in r['symbol']]

    if sort_key == 'apr_desc':
        ranking = sorted(ranking, key=lambda r: (r.get('apr') is None, -(r.get('apr') or 0)))
    elif sort_key == 'apr_asc':
        ranking = sorted(ranking, key=lambda r: (r.get('apr') is None, r.get('apr') or 0))
    elif sort_key == 'rate_desc':
        ranking = sorted(ranking, key=lambda r: (r.get('funding_rate') is None,
                                                  -(r.get('funding_rate') or 0)))
    else:
        ranking = sorted(ranking, key=lambda r: (r.get('apr') is None, -abs(r.get('apr') or 0)))

    return jsonify({'success': True, 'data': ranking[:limit], 'total': len(ranking)})


@api_bp.route('/funding/spreads')
@api_auth_required
def funding_spreads():
    """跨所费率差（支持 ?exchange= 和 ?symbol= 过滤）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    limit = _int_arg('limit', 100, 1000)
    exchange = request.args.get('exchange', '').lower()
    symbol = request.args.get('symbol', '').upper()
    rows = market_monitor.last_scan.get('cross_spreads', [])
    if exchange:
        rows = [r for r in rows if r['exchange_a'] == exchange or r['exchange_b'] == exchange]
    if symbol:
        exact = [r for r in rows if r['symbol'].split('/')[0] == symbol]
        rows = exact or [r for r in rows if symbol in r['symbol']]
    return jsonify({'success': True, 'data': rows[:limit], 'total': len(rows)})


@api_bp.route('/basis')
@api_auth_required
def basis():
    """基差列表（支持 ?exchange= 和 ?symbol= 过滤）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    limit = _int_arg('limit', 100, 1000)
    exchange = request.args.get('exchange', '').lower()
    symbol = request.args.get('symbol', '').upper()
    rows = market_monitor.last_scan.get('basis', [])
    if exchange:
        rows = [r for r in rows if r['exchange'] == exchange]
    if symbol:
        exact = [r for r in rows if r['symbol'].split('/')[0] == symbol]
        rows = exact or [r for r in rows if symbol in r['symbol']]
    return jsonify({'success': True, 'data': rows[:limit], 'total': len(rows)})


@api_bp.route('/stablecoins')
@api_auth_required
def stablecoins():
    """锚定资产状态（稳定币/黄金/股票）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    rows = market_monitor.last_scan.get('depeg', [])
    category = request.args.get('category', '')
    if category:
        rows = [r for r in rows if r.get('category') == category]
    return jsonify({'success': True, 'data': rows})


@api_bp.route('/stablecoins/history')
@api_auth_required
def stablecoin_history():
    """稳定币历史价格（?symbol=USDC/USDT&hours=24）"""
    db_manager = current_app.config['DB_MANAGER']
    symbol = request.args.get('symbol', 'USDC')
    hours = _int_arg('hours', 24, 24 * 30)
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


@api_bp.route('/issuer-spreads')
@api_auth_required
def issuer_spreads():
    """跨发行商价差（同一标的的不同 RWA 版本）"""
    market_monitor = current_app.config['MARKET_MONITOR']
    rows = market_monitor.last_scan.get('issuer_spreads', [])
    return jsonify({'success': True, 'data': rows, 'total': len(rows)})


@api_bp.route('/alerts')
@api_auth_required
def alerts():
    """告警记录（?hours=24&limit=100&level=critical&type=stablecoin_depeg）"""
    alert_manager = current_app.config['ALERT_MANAGER']
    hours = _int_arg('hours', 24, 24 * 30)
    limit = _int_arg('limit', 100, 1000)
    level = request.args.get('level', '')
    alert_type = request.args.get('type', '')

    rows = alert_manager.get_recent_alerts(
        limit=limit, hours=hours, level=level, alert_type=alert_type
    )
    total = alert_manager.count_alerts(hours=hours, level=level, alert_type=alert_type)
    return jsonify({'success': True, 'data': rows, 'total': total})


@api_bp.route('/alerts/stats')
@api_auth_required
def alert_stats():
    """告警统计"""
    alert_manager = current_app.config['ALERT_MANAGER']
    hours = _int_arg('hours', 24, 24 * 30)
    return jsonify({'success': True, 'data': alert_manager.get_alert_stats(hours)})
