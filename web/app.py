"""
Flask Web应用
"""
import os
from functools import wraps
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from loguru import logger
from backtesting import BacktestEngine, DataLoader, ResultsAnalyzer
from utils.performance import performance_monitor, error_collector
import time


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

    # 认证配置 - 从环境变量读取
    WEB_USERNAME = os.getenv('WEB_USERNAME', 'admin')
    WEB_PASSWORD_HASH = os.getenv('WEB_PASSWORD_HASH', generate_password_hash('admin123'))

    # 登录装饰器
    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not session.get('logged_in'):
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return decorated_function

    # API认证装饰器
    def api_auth_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not session.get('logged_in'):
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401
            return f(*args, **kwargs)
        return decorated_function

    # 登录页面
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """登录页面"""
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')

            if username == WEB_USERNAME and check_password_hash(WEB_PASSWORD_HASH, password):
                session['logged_in'] = True
                session['username'] = username
                logger.info(f"User {username} logged in successfully")
                return redirect(url_for('index'))
            else:
                logger.warning(f"Failed login attempt for username: {username}")
                return render_template('login.html', error='Invalid username or password')

        return render_template('login.html')

    # 登出
    @app.route('/logout')
    def logout():
        """登出"""
        username = session.get('username', 'Unknown')
        session.clear()
        logger.info(f"User {username} logged out")
        return redirect(url_for('login'))

    # 首页
    @app.route('/')
    @login_required
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

    # 机会监控页面
    @app.route('/opportunities')
    @login_required
    def opportunities_page():
        """机会监控页面"""
        return render_template('opportunities.html')

    # 持仓管理页面
    @app.route('/positions')
    @login_required
    def positions_page():
        """持仓管理页面"""
        return render_template('positions.html')

    # 配置页面
    @app.route('/config')
    @login_required
    def config_page():
        """配置页面"""
        return render_template('config.html')

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

    @app.route('/api/health')
    def api_health():
        """健康检查API"""
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

    @app.route('/api/errors')
    @api_auth_required
    def api_errors():
        """获取最近错误"""
        try:
            summary = error_collector.get_error_summary()
            return jsonify({'success': True, 'data': summary})
        except Exception as e:
            logger.error(f"Error getting error summary: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/opportunities')
    @api_auth_required
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
    @api_auth_required
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
    @api_auth_required
    def api_config():
        """配置API"""
        try:
            configs = {}
            if config_manager:
                # 获取一些关键配置
                configs = {
                    'total_capital': config_manager.get('global', 'total_capital', 100000),
                    'max_positions': config_manager.get('global', 'max_positions', 10),
                    'strategy1_enabled': config_manager.get('strategy1', 'enabled', True),
                    'strategy2a_enabled': config_manager.get('strategy2a', 'enabled', True),
                    'strategy2b_enabled': config_manager.get('strategy2b', 'enabled', True),
                }
            return jsonify({'success': True, 'data': configs})
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/config/update', methods=['POST'])
    @api_auth_required
    def api_config_update():
        """更新配置API"""
        try:
            data = request.get_json()
            category = data.get('category')
            key = data.get('key')
            value = data.get('value')

            if not all([category, key, value is not None]):
                return jsonify({'success': False, 'error': 'Missing required fields'})

            config_manager.set(f"{category}.{key}", value)
            return jsonify({'success': True, 'message': 'Config updated'})
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/close_position/<int:position_id>', methods=['POST'])
    @api_auth_required
    def api_close_position(position_id):
        """平仓API"""
        try:
            if strategy_executor:
                result = strategy_executor.close_position(position_id)
                return jsonify({'success': True, 'data': result})
            return jsonify({'success': False, 'error': 'Strategy executor not available'})
        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/export/positions', methods=['GET'])
    @api_auth_required
    def api_export_positions():
        """导出持仓数据"""
        try:
            import csv
            from io import StringIO
            from flask import make_response

            output = StringIO()
            writer = csv.writer(output)

            # 写入表头
            writer.writerow(['ID', 'Symbol', 'Strategy', 'Position Size', 'Current PnL',
                           'Realized PnL', 'Fees', 'Status', 'Open Time', 'Close Time'])

            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM positions ORDER BY id DESC LIMIT 1000")
                for row in cursor.fetchall():
                    writer.writerow(row)

            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = 'attachment; filename=positions_export.csv'
            return response
        except Exception as e:
            logger.error(f"Error exporting positions: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/export/funding_rates', methods=['GET'])
    @api_auth_required
    def api_export_funding_rates():
        """导出资金费率数据"""
        try:
            import csv
            from io import StringIO
            from flask import make_response

            output = StringIO()
            writer = csv.writer(output)

            writer.writerow(['ID', 'Exchange', 'Symbol', 'Funding Rate', 'Timestamp'])

            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM funding_rates ORDER BY timestamp DESC LIMIT 10000")
                for row in cursor.fetchall():
                    writer.writerow(row)

            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = 'attachment; filename=funding_rates_export.csv'
            return response
        except Exception as e:
            logger.error(f"Error exporting funding rates: {e}")
            return jsonify({'success': False, 'error': str(e)})

    # 回测API
    @app.route('/api/backtest/run', methods=['POST'])
    @api_auth_required
    def api_run_backtest():
        """运行回测"""
        try:
            data = request.get_json()
            start_date = data.get('start_date')
            end_date = data.get('end_date')
            initial_capital = data.get('initial_capital', 100000)
            strategies = data.get('strategies', ['strategy1', 'strategy2a'])

            if not all([start_date, end_date]):
                return jsonify({'success': False, 'error': 'Missing required fields'})

            # 创建回测引擎
            backtest_engine = BacktestEngine(db_manager, config_manager)

            # 运行回测
            results = backtest_engine.run_backtest(
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                strategies=strategies
            )

            # 保存结果
            name = data.get('name', f'backtest_{start_date}_{end_date}')
            backtest_engine.save_backtest_results(results, name)

            return jsonify({'success': True, 'data': results})

        except Exception as e:
            logger.error(f"Error running backtest: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/backtest/data_range', methods=['GET'])
    @api_auth_required
    def api_backtest_data_range():
        """获取可用的数据范围"""
        try:
            data_loader = DataLoader(db_manager)
            date_range = data_loader.get_available_date_range()
            return jsonify({'success': True, 'data': date_range})
        except Exception as e:
            logger.error(f"Error getting data range: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/backtest/results', methods=['GET'])
    @api_auth_required
    def api_backtest_results():
        """获取历史回测结果列表"""
        try:
            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, name, timestamp, initial_capital, final_capital,
                           total_trades, win_rate, roi, max_drawdown
                    FROM backtest_results
                    ORDER BY timestamp DESC
                    LIMIT 50
                """)
                columns = [desc[0] for desc in cursor.description]
                results = [dict(zip(columns, row)) for row in cursor.fetchall()]

            return jsonify({'success': True, 'data': results})
        except Exception as e:
            logger.error(f"Error getting backtest results: {e}")
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
