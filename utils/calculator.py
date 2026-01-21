"""
工具函数：收益和风险计算
"""
from typing import Dict, Any


def estimate_slippage(depth: float, trade_amount: float) -> float:
    """
    根据订单簿深度估算滑点
    depth: 前5档总深度
    trade_amount: 交易数量
    """
    if trade_amount < depth * 0.1:
        return 0  # 深度充足，无滑点
    elif trade_amount < depth * 0.5:
        return trade_amount * 0.0001  # 小滑点
    else:
        return trade_amount * 0.0005  # 较大滑点


def calculate_score(net_profit_pct: float, risk_factor: float, bonus_factor: float) -> float:
    """
    综合评分 0-100
    net_profit_pct: 净收益率（越高越好）
    risk_factor: 风险因子（价差/基差，越低越好）
    bonus_factor: 加分项（如年化费率）
    """
    profit_score = min(net_profit_pct * 10000, 50)  # 最高50分
    risk_score = max(0, 30 - risk_factor * 1000)  # 最高30分
    bonus_score = min(bonus_factor / 10, 20)  # 最高20分

    return profit_score + risk_score + bonus_score


def calculate_cross_exchange_funding_profit(
    position_size: float,
    short_rate: float,
    long_rate: float,
    short_taker_fee: float,
    long_taker_fee: float,
    short_maker_fee: float,
    long_maker_fee: float,
    long_slippage: float,
    short_slippage: float
) -> Dict[str, Any]:
    """
    计算跨交易所资金费率套利收益
    """
    # 单期收益（8小时）
    funding_income = position_size * (short_rate - long_rate)

    # 开仓成本
    long_open_fee = position_size * long_taker_fee
    short_open_fee = position_size * short_taker_fee

    # 平仓成本（预估）
    long_close_fee = position_size * long_maker_fee
    short_close_fee = position_size * short_maker_fee

    # 总成本
    total_cost = (long_open_fee + short_open_fee + long_close_fee +
                 short_close_fee + long_slippage + short_slippage)

    # 净收益
    net_profit = funding_income - total_cost
    net_profit_pct = net_profit / position_size

    return {
        'funding_income': funding_income,
        'total_cost': total_cost,
        'net_profit': net_profit,
        'net_profit_pct': net_profit_pct,
        'open_fees': long_open_fee + short_open_fee,
        'close_fees': long_close_fee + short_close_fee,
        'slippage': long_slippage + short_slippage
    }


def calculate_spot_futures_funding_profit(
    position_size: float,
    funding_rate: float,
    spot_taker_fee: float,
    futures_taker_fee: float,
    spot_maker_fee: float,
    futures_maker_fee: float
) -> Dict[str, Any]:
    """
    计算现货-期货资金费率套利收益
    """
    # 单期收益（8小时）
    funding_income = position_size * funding_rate

    # 开仓成本
    spot_open_fee = position_size * spot_taker_fee
    futures_open_fee = position_size * futures_taker_fee

    # 平仓成本
    spot_close_fee = position_size * spot_maker_fee
    futures_close_fee = position_size * futures_maker_fee

    # 总成本
    total_cost = spot_open_fee + futures_open_fee + spot_close_fee + futures_close_fee

    # 净收益
    net_profit = funding_income - total_cost
    net_profit_pct = net_profit / position_size

    return {
        'funding_income': funding_income,
        'total_cost': total_cost,
        'net_profit': net_profit,
        'net_profit_pct': net_profit_pct,
        'open_fees': spot_open_fee + futures_open_fee,
        'close_fees': spot_close_fee + futures_close_fee
    }


def calculate_basis_arbitrage_profit(
    position_size: float,
    basis: float,
    funding_rate: float,
    estimated_hold_periods: int,
    spot_taker_fee: float,
    futures_taker_fee: float,
    spot_maker_fee: float,
    futures_maker_fee: float
) -> Dict[str, Any]:
    """
    计算基差套利收益
    estimated_hold_periods: 预估持仓期数（每期8小时）
    """
    # 基差收敛收益（假设基差完全归零）
    basis_income = position_size * basis

    # 开仓成本
    spot_open_fee = position_size * spot_taker_fee
    futures_open_fee = position_size * futures_taker_fee

    # 平仓成本
    spot_close_fee = position_size * spot_maker_fee
    futures_close_fee = position_size * futures_maker_fee

    # 总手续费
    total_fee = spot_open_fee + futures_open_fee + spot_close_fee + futures_close_fee

    # 资金费率影响（持仓期间可能收取或支付）
    estimated_funding_cost = position_size * funding_rate * estimated_hold_periods

    # 净收益
    net_profit = basis_income - total_fee - estimated_funding_cost
    net_profit_pct = net_profit / position_size

    return {
        'basis_income': basis_income,
        'total_fee': total_fee,
        'estimated_funding_cost': estimated_funding_cost,
        'net_profit': net_profit,
        'net_profit_pct': net_profit_pct,
        'open_fees': spot_open_fee + futures_open_fee,
        'close_fees': spot_close_fee + futures_close_fee
    }
