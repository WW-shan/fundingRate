"""
页面路由
"""
from flask import Blueprint, render_template
from web.auth import login_required

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
@login_required
def index():
    """首页 - 仪表盘"""
    return render_template('index.html')


@pages_bp.route('/opportunities')
@login_required
def opportunities():
    """机会监控页面"""
    return render_template('opportunities.html')


@pages_bp.route('/positions')
@login_required
def positions():
    """持仓管理页面"""
    return render_template('positions.html')


@pages_bp.route('/config')
@login_required
def config():
    """配置页面"""
    return render_template('config.html')
