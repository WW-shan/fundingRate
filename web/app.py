"""
Flask Web 应用（监控版）
"""
import os
from flask import Flask
from loguru import logger


def create_app(config_manager, db_manager, data_collector, market_monitor, alert_manager,
               liquidation_monitor=None):
    """创建 Flask 应用"""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

    app.config['CONFIG_MANAGER'] = config_manager
    app.config['DB_MANAGER'] = db_manager
    app.config['DATA_COLLECTOR'] = data_collector
    app.config['MARKET_MONITOR'] = market_monitor
    app.config['ALERT_MANAGER'] = alert_manager
    app.config['LIQUIDATION_MONITOR'] = liquidation_monitor

    from web.routes import auth_bp, pages_bp, api_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    logger.info("Flask app created (monitor mode)")
    return app


def run_web_server(app, host='0.0.0.0', port=5000):
    logger.info(f"Starting web server on {host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
