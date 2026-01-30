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
        """获取所有配置API"""
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
                
                # 风控配置
                configs['risk'] = {
                    'max_position_size_per_trade': config_manager.get('risk', 'max_position_size_per_trade', 30),
                    'max_drawdown': config_manager.get('risk', 'max_drawdown', 0.10),
                    'dynamic_position_enabled': config_manager.get('risk', 'dynamic_position_enabled', True),
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
                return jsonify({'success': False, 'error': '缺少必需字段'})

            # 正确调用ConfigManager的set方法（category和key是分开的参数）
            config_manager.set(category, key, value, is_hot_reload=True)
            
            logger.info(f"Config updated via web: {category}.{key} = {value}")
            return jsonify({'success': True, 'message': '配置已更新'})
        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/exchanges')
    @api_auth_required
    def api_exchanges():
        """获取交易所账户列表"""
        try:
            exchanges = db_manager.execute_query(
                "SELECT exchange_name, is_active FROM exchange_accounts ORDER BY exchange_name"
            )
            return jsonify({'success': True, 'data': exchanges})
        except Exception as e:
            logger.error(f"Error getting exchanges: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/exchanges/add', methods=['POST'])
    @api_auth_required
    def api_exchanges_add():
        """添加交易所账户"""
        try:
            data = request.get_json()
            exchange_name = data.get('exchange_name', '').lower().strip()
            api_key = data.get('api_key', '').strip()
            api_secret = data.get('api_secret', '').strip()
            passphrase = data.get('passphrase', '').strip()

            if not exchange_name or not api_key or not api_secret:
                return jsonify({'success': False, 'error': '请填写完整信息'})

            if exchange_name not in ['binance', 'okx', 'bybit', 'gate', 'bitget']:
                return jsonify({'success': False, 'error': '不支持的交易所'})

            if exchange_name in ['okx', 'bitget'] and not passphrase:
                return jsonify({'success': False, 'error': f'{exchange_name.upper()} 需要 Passphrase'})

            # 插入或更新数据库
            db_manager.execute_query(
                """
                INSERT INTO exchange_accounts (exchange_name, api_key, api_secret, passphrase, is_active)
                VALUES (?, ?, ?, ?, TRUE)
                ON CONFLICT(exchange_name) DO UPDATE SET
                    api_key = excluded.api_key,
                    api_secret = excluded.api_secret,
                    passphrase = excluded.passphrase,
                    is_active = TRUE
                """,
                (exchange_name, api_key, api_secret, passphrase)
            )

            logger.info(f"Exchange account added: {exchange_name}")
            return jsonify({'success': True, 'message': '添加成功，请重启应用生效'})
        except Exception as e:
            logger.error(f"Error adding exchange: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/exchanges/delete', methods=['POST'])
    @api_auth_required
    def api_exchanges_delete():
        """删除交易所账户"""
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

    @app.route('/api/exchanges/status')
    @api_auth_required
    def api_exchanges_status():
        """获取交易所数据采集状态"""
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
