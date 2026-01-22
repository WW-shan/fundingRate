"""
路由模块
"""
from .auth import auth_bp
from .pages import pages_bp
from .api import api_bp
from .config_api import config_api_bp
from .exchange_api import exchange_api_bp

__all__ = ['auth_bp', 'pages_bp', 'api_bp', 'config_api_bp', 'exchange_api_bp']
