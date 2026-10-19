"""
认证相关路由
"""
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from loguru import logger
import os
from werkzeug.security import check_password_hash

auth_bp = Blueprint('auth', __name__)

WEB_USERNAME = os.getenv('WEB_USERNAME', 'admin')
WEB_PASSWORD_HASH = os.getenv('WEB_PASSWORD_HASH')


def check_password(password_hash, password):
    """校验密码；未配置哈希时使用 WEB_PASSWORD（默认 admin123）"""
    if not password_hash:
        return password == os.getenv('WEB_PASSWORD', 'admin123')
    try:
        return check_password_hash(password_hash, password)
    except Exception:
        logger.error("WEB_PASSWORD_HASH 格式无效")
        return False


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """登录"""
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()

        if username == WEB_USERNAME and check_password(WEB_PASSWORD_HASH, password):
            session['logged_in'] = True
            session['username'] = username
            logger.info(f"User {username} logged in")

            if request.is_json:
                return jsonify({'success': True})
            return redirect(url_for('pages.index'))
        else:
            logger.warning(f"Failed login attempt for user: {username}")
            if request.is_json:
                return jsonify({'success': False, 'error': '用户名或密码错误'})
            return render_template('login.html', error='用户名或密码错误')

    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    """登出"""
    username = session.get('username', 'unknown')
    session.clear()
    logger.info(f"User {username} logged out")
    return redirect(url_for('auth.login'))
