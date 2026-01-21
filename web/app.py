"""
Flask Web应用
"""
import os
from flask import Flask, render_template, jsonify, request
from loguru import logger


def create_app(config_manager, db_manager, data_collector, opportunity_monitor, strategy_executor, risk_manager):
    """创建Flask应用"""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

    # 存储系统组件的引用
    app.config['CONFIG_MANAGER'] = config_manager
    app.config['DB_MANAGER'] = db_manager
    app.config['DATA_COLLECTOR'] = data_collector
    app.config['OPPORTUNITY_MONITOR'] = opportunity_monitor
    app.config['STRATEGY_EXECUTOR'] = strategy_executor
    app.config['RISK_MANAGER'] = risk_manager

    # 首页
    @app.route('/')
    def index():
        """首页 - 仪表盘"""
        try:
            # 获取持仓数据
            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as open_positions
                    FROM positions
                """)
                position_stats = cursor.fetchone()

            return render_template('index.html',
                                 position_stats=position_stats or {'total': 0, 'open_positions': 0})
        except Exception as e:
            logger.error(f"Error in index route: {e}")
            return render_template('index.html', position_stats={'total': 0, 'open_positions': 0})

    # API路由
    @app.route('/api/status')
    def api_status():
        """系统状态API"""
        return jsonify({
            'status': 'running',
            'trading_enabled': os.getenv('ENABLE_TRADING', 'False').lower() == 'true',
            'exchanges_connected': len(data_collector.exchanges) if data_collector else 0,
            'market_data_symbols': len(data_collector.market_data) if data_collector else 0
        })

    @app.route('/api/opportunities')
    def api_opportunities():
        """当前机会API"""
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

    @app.route('/api/positions')
    def api_positions():
        """持仓API"""
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

    @app.route('/api/config')
    def api_config():
        """配置API"""
        try:
            configs = {}
            if config_manager:
                # 获取一些关键配置
                configs = {
                    'total_capital': config_manager.get('global.total_capital', 100000),
                    'max_positions': config_manager.get('global.max_positions', 10),
                    'strategy1_enabled': config_manager.get('strategy1.enabled', True),
                    'strategy2a_enabled': config_manager.get('strategy2a.enabled', True),
                    'strategy2b_enabled': config_manager.get('strategy2b.enabled', True),
                }
            return jsonify({'success': True, 'data': configs})
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return jsonify({'success': False, 'error': str(e)})

    # 错误处理
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(500)
    def internal_error(error):
        logger.error(f"Internal error: {error}")
        return jsonify({'error': 'Internal server error'}), 500

    return app


def run_web_server(app, host='0.0.0.0', port=5000):
    """运行Web服务器"""
    try:
        logger.info(f"Starting Web server on http://{host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Failed to start Web server: {e}")
