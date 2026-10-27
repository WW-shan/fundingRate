"""
页面路由
"""
from flask import Blueprint, render_template
from web.auth import login_required

pages_bp = Blueprint('pages', __name__)


@pages_bp.route('/')
@login_required
def index():
    """总览面板"""
    return render_template('index.html')


@pages_bp.route('/funding')
@login_required
def funding():
    """资金费率监控（APR 排行 + 跨所差）"""
    return render_template('funding.html')


@pages_bp.route('/stablecoin')
@login_required
def stablecoin():
    """稳定币脱锚监控"""
    return render_template('stablecoin.html')


@pages_bp.route('/liquidations')
@login_required
def liquidations():
    """跨交易所爆仓量监控"""
    return render_template('liquidations.html')


@pages_bp.route('/alerts')
@login_required
def alerts():
    """告警历史"""
    return render_template('alerts.html')
