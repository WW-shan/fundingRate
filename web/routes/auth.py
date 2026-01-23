"""
认证相关路由
"""
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from loguru import logger
import os
import hashlib

auth_bp = Blueprint('auth', __name__)

# 从环境变量读取认证配置
WEB_USERNAME = os.getenv('WEB_USERNAME', 'admin')
WEB_PASSWORD_HASH = os.getenv('WEB_PASSWORD_HASH')

def check_password(password_hash, password):
    """
    检查密码 - 兼容多种哈希方法
    支持: scrypt, pbkdf2:sha256, sha256
    """
    if not password_hash:
        # 如果没有设置密码哈希,使用默认密码 admin123
        return password == 'admin123'

    try:
        # 方法1: 尝试使用 werkzeug (如果 scrypt 可用)
        from werkzeug.security import check_password_hash
        return check_password_hash(password_hash, password)
    except (ImportError, AttributeError) as e:
        logger.warning(f"werkzeug check_password_hash 不可用: {e}, 使用备用方法")

        # 方法2: 手动解析哈希格式
        if password_hash.startswith('scrypt:'):
            logger.error("系统不支持 scrypt,请重新生成密码哈希")
            logger.info("临时使用默认密码: admin123")
            return password == 'admin123'

        elif password_hash.startswith('pbkdf2:'):
            # pbkdf2:sha256:iterations$salt$hash
            try:
                parts = password_hash.split('$')
                if len(parts) >= 3:
                    method_info = parts[0]  # pbkdf2:sha256:iterations
                    salt = parts[1].encode('utf-8')
                    stored_hash = parts[2]

                    # 提取迭代次数
                    iterations = 260000  # 默认值
                    if ':' in method_info:
                        method_parts = method_info.split(':')
                        if len(method_parts) >= 3:
                            iterations = int(method_parts[2])

                    # 计算哈希
                    computed_hash = hashlib.pbkdf2_hmac(
                        'sha256',
                        password.encode('utf-8'),
                        salt,
                        iterations
                    ).hex()

                    return computed_hash == stored_hash
            except Exception as e:
                logger.error(f"pbkdf2 验证失败: {e}")
                return False

        elif password_hash.startswith('sha256:'):
            # 简单 sha256
            computed = hashlib.sha256(password.encode('utf-8')).hexdigest()
            return password_hash[7:] == computed

        else:
            # 明文比较 (不推荐)
            logger.warning("使用明文密码比较,不安全!")
            return password_hash == password


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
