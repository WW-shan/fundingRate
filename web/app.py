"""
Flask Web应用 - 重构版
使用Blueprint模块化架构
"""
import os
from flask import Flask
from werkzeug.security import generate_password_hash
from loguru import logger


def create_app(config_manager, db_manager, data_collector, opportunity_monitor, strategy_executor, risk_manager):
    """创建Flask应用"""
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

    # 存储系统组件的引用到app.config，供Blueprint使用
    app.config['CONFIG_MANAGER'] = config_manager
    app.config['DB_MANAGER'] = db_manager
    app.config['DATA_COLLECTOR'] = data_collector
    app.config['OPPORTUNITY_MONITOR'] = opportunity_monitor
    app.config['STRATEGY_EXECUTOR'] = strategy_executor
    app.config['RISK_MANAGER'] = risk_manager

    # 注册所有Blueprint
    from web.routes import auth_bp, pages_bp, api_bp, config_api_bp, exchange_api_bp
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(config_api_bp)
    app.register_blueprint(exchange_api_bp)

    logger.info("Flask app created with Blueprint architecture")
    logger.info(f"Registered blueprints: {[bp.name for bp in app.blueprints.values()]}")

    return app


def run_app(app, host='0.0.0.0', port=5000):
    """运行Flask应用"""
    logger.info(f"Starting web server on {host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


# 兼容旧代码的别名
run_web_server = run_app
