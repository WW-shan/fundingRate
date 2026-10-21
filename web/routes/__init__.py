"""
路由模块
"""
from .auth import auth_bp
from .pages import pages_bp
from .api import api_bp

__all__ = ['auth_bp', 'pages_bp', 'api_bp']
