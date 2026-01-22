"""
认证装饰器
"""
from functools import wraps
from flask import session, redirect, url_for, jsonify


def login_required(f):
    """页面登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def api_auth_required(f):
    """API认证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated_function
