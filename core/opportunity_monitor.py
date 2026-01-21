"""
机会监控系统
实时扫描套利机会，计算最优策略组合
"""
import time
import threading
import uuid
from typing import Dict, List, Any
from datetime import datetime
from loguru import logger
from config import ConfigManager
from database import DatabaseManager
from core.data_collector import DataCollector
from utils.calculator import (
    estimate_slippage,
    calculate_score,
    calculate_cross_exchange_funding_profit,
    calculate_spot_futures_funding_profit,
    calculate_basis_arbitrage_profit
)


class OpportunityMonitor:
    """机会监控系统"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager,
                 data_collector: DataCollector):
        self.config = config_manager
        self.db = db_manager
        self.data_collector = data_collector
        self.running = False
        self.opportunities = []  # 当前发现的机会列表
        self.opportunity_callbacks = []  # 机会回调函数列表

    def start(self):
        """启动机会监控"""
        logger.info("Starting opportunity monitor...")
        self.running = True

        threading.Thread(target=self._monitoring_loop, daemon=True).start()

        logger.info("Opportunity monitor started")

    def stop(self):
        """停止机会监控"""
        logger.info("Stopping opportunity monitor...")
        self.running = False

    def register_callback(self, callback):
        """注册机会发现回调函数"""
        self.opportunity_callbacks.append(callback)

    def _monitoring_loop(self):
        """监控循环"""
        interval = self.config.get('global', 'opportunity_scan_interval', 10)

        while self.running:
            try:
                self._scan_opportunities()
                time.sleep(interval)
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(interval)

    def _scan_opportunities(self):
        """扫描所有套利机会"""
        market_data = self.data_collector.get_market_data()

        if not market_data:
            logger.debug("No market data available yet")
            return

        new_opportunities = []

        # 遍历所有交易对
        for symbol, exchanges_data in market_data.items():
            # 策略1：跨交易所资金费率套利
            if self.config.get('strategy1', 'enabled', True):
                opps = self._calculate_cross_exchange_funding_opportunities(symbol, exchanges_data)
                new_opportunities.extend(opps)

            # 策略2A：现货期货资金费率套利
            if self.config.get('strategy2a', 'enabled', True):
                opps = self._calculate_spot_futures_funding_opportunities(symbol, exchanges_data)
                new_opportunities.extend(opps)

            # 策略2B：基差套利
            if self.config.get('strategy2b', 'enabled', True):
                opps = self._calculate_basis_arbitrage_opportunities(symbol, exchanges_data)
                new_opportunities.extend(opps)

        # 更新机会列表
        self.opportunities = sorted(new_opportunities, key=lambda x: x['score'], reverse=True)

        logger.info(f"Found {len(self.opportunities)} opportunities")

        # 触发回调
        for callback in self.opportunity_callbacks:
            try:
                callback(self.opportunities)
            except Exception as e:
                logger.error(f"Error in opportunity callback: {e}")

    def _calculate_cross_exchange_funding_opportunities(
        self, symbol: str, exchanges_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """计算跨交易所资金费率套利机会"""
        opportunities = []
        exchanges = list(exchanges_data.keys())

        # 获取配置
        pair_config = self.config.get_pair_config(symbol, strategy_prefix='s1')
        min_funding_diff = pair_config.get('s1_min_funding_diff', 0.0005)
        min_profit_rate = self.config.get('strategy1', 'min_profit_rate', 0.0003)
        position_size = pair_config.get('s1_position_size', 10000)
        max_price_diff = self.config.get('strategy1', 'max_price_diff', 0.02)

        # 遍历所有交易所组合
        for i, exchange_long in enumerate(exchanges):
            for exchange_short in exchanges[i+1:]:
                try:
                    long_data = exchanges_data[exchange_long]
                    short_data = exchanges_data[exchange_short]

                    # 检查数据完整性
                    if not all(k in long_data for k in ['funding_rate', 'futures_ask', 'futures_bid', 'taker_fee', 'maker_fee']):
                        continue
                    if not all(k in short_data for k in ['funding_rate', 'futures_ask', 'futures_bid', 'taker_fee', 'maker_fee']):
                        continue

                    # 计算资金费率差
                    funding_diff = short_data['funding_rate'] - long_data['funding_rate']

                    if funding_diff < min_funding_diff:
                        continue

                    # 计算价格差异
                    long_price = long_data['futures_ask']
                    short_price = short_data['futures_bid']
                    price_diff_pct = abs(long_price - short_price) / long_price

                    if price_diff_pct > max_price_diff:
                        continue  # 价格差异过大，可能异常

                    # 计算滑点
                    trade_amount_btc = position_size / long_price
                    long_slippage = estimate_slippage(long_data.get('futures_depth_5', 0), trade_amount_btc)
                    short_slippage = estimate_slippage(short_data.get('futures_depth_5', 0), trade_amount_btc)

                    # 计算收益
                    profit = calculate_cross_exchange_funding_profit(
                        position_size=position_size,
                        short_rate=short_data['funding_rate'],
                        long_rate=long_data['funding_rate'],
                        short_taker_fee=short_data['taker_fee'],
                        long_taker_fee=long_data['taker_fee'],
                        short_maker_fee=short_data['maker_fee'],
                        long_maker_fee=long_data['maker_fee'],
                        long_slippage=long_slippage,
                        short_slippage=short_slippage
                    )

                    if profit['net_profit_pct'] < min_profit_rate:
                        continue

                    # 计算年化费率差
                    annual_funding_diff = funding_diff * 3 * 365  # 每天3次

                    # 计算评分
                    score = calculate_score(
                        profit['net_profit_pct'],
                        price_diff_pct,
                        annual_funding_diff
                    )

                    opportunity = {
                        'id': str(uuid.uuid4()),
                        'type': 'funding_rate_cross_exchange',
                        'risk_level': 'low',
                        'score': score,
                        'symbol': symbol,
                        'long_exchange': exchange_long,
                        'short_exchange': exchange_short,
                        'funding_diff': funding_diff,
                        'funding_diff_annual': annual_funding_diff,
                        'position_size': position_size,
                        'expected_return': profit['net_profit'],
                        'expected_return_pct': profit['net_profit_pct'],
                        'long_entry_price': long_price,
                        'short_entry_price': short_price,
                        'price_diff_pct': price_diff_pct,
                        'details': profit,
                        'detected_at': datetime.now().isoformat(),
                        'status': 'pending'
                    }

                    opportunities.append(opportunity)

                except Exception as e:
                    logger.debug(f"Error calculating cross exchange opportunity: {e}")

        return opportunities

    def _calculate_spot_futures_funding_opportunities(
        self, symbol: str, exchanges_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """计算现货期货资金费率套利机会"""
        opportunities = []

        for exchange, data in exchanges_data.items():
            try:
                # 检查数据完整性
                required_fields = ['funding_rate', 'spot_ask', 'futures_bid', 'spot_price', 'futures_price', 'taker_fee', 'maker_fee']
                if not all(k in data for k in required_fields):
                    continue

                # 获取配置
                pair_config = self.config.get_pair_config(symbol, exchange, 's2a')
                min_funding_rate = pair_config.get('s2a_min_funding_rate', 0.30)  # 年化30%
                max_basis_deviation = pair_config.get('s2a_max_basis_deviation', 0.01)
                position_size = pair_config.get('s2a_position_size', 10000)

                # 计算年化资金费率
                annual_funding_rate = data['funding_rate'] * 3 * 365

                if annual_funding_rate < min_funding_rate:
                    continue

                # 计算基差
                basis = (data['futures_price'] - data['spot_price']) / data['spot_price']

                if abs(basis) > max_basis_deviation:
                    continue  # 基差风险过大

                # 计算收益
                profit = calculate_spot_futures_funding_profit(
                    position_size=position_size,
                    funding_rate=data['funding_rate'],
                    spot_taker_fee=data['taker_fee'],
                    futures_taker_fee=data['taker_fee'],
                    spot_maker_fee=data['maker_fee'],
                    futures_maker_fee=data['maker_fee']
                )

                if profit['net_profit_pct'] <= 0:
                    continue  # 单期必须盈利

                # 计算评分
                score = calculate_score(
                    profit['net_profit_pct'],
                    abs(basis),
                    annual_funding_rate
                )

                opportunity = {
                    'id': str(uuid.uuid4()),
                    'type': 'funding_rate_spot_futures',
                    'risk_level': 'low',
                    'score': score,
                    'symbol': symbol,
                    'exchange': exchange,
                    'annual_funding_rate': annual_funding_rate,
                    'basis': basis,
                    'position_size': position_size,
                    'expected_return': profit['net_profit'],
                    'expected_return_pct': profit['net_profit_pct'],
                    'spot_price': data['spot_ask'],
                    'futures_price': data['futures_bid'],
                    'details': profit,
                    'detected_at': datetime.now().isoformat(),
                    'status': 'pending'
                }

                opportunities.append(opportunity)

            except Exception as e:
                logger.debug(f"Error calculating spot futures funding opportunity: {e}")

        return opportunities

    def _calculate_basis_arbitrage_opportunities(
        self, symbol: str, exchanges_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """计算基差套利机会"""
        opportunities = []

        for exchange, data in exchanges_data.items():
            try:
                # 检查数据完整性
                required_fields = ['spot_ask', 'futures_bid', 'spot_price', 'futures_price', 'funding_rate', 'taker_fee', 'maker_fee']
                if not all(k in data for k in required_fields):
                    continue

                # 获取配置
                pair_config = self.config.get_pair_config(symbol, exchange, 's2b')
                min_basis = pair_config.get('s2b_min_basis', 0.02)
                target_return = pair_config.get('s2b_target_return', 0.015)
                position_size = pair_config.get('s2b_position_size', 8000)

                # 计算基差
                basis = (data['futures_price'] - data['spot_price']) / data['spot_price']

                if abs(basis) < min_basis:
                    continue

                # 计算收益（假设持仓3天，9期资金费率）
                estimated_hold_periods = 9
                profit = calculate_basis_arbitrage_profit(
                    position_size=position_size,
                    basis=basis,
                    funding_rate=data['funding_rate'],
                    estimated_hold_periods=estimated_hold_periods,
                    spot_taker_fee=data['taker_fee'],
                    futures_taker_fee=data['taker_fee'],
                    spot_maker_fee=data['maker_fee'],
                    futures_maker_fee=data['maker_fee']
                )

                if profit['net_profit_pct'] < target_return:
                    continue

                # 计算评分
                score = calculate_score(
                    profit['net_profit_pct'],
                    abs(basis),
                    0  # 基差套利没有bonus因子
                )

                # 风险等级
                if abs(basis) < 0.03:
                    risk_level = 'medium'
                else:
                    risk_level = 'high'

                opportunity = {
                    'id': str(uuid.uuid4()),
                    'type': 'basis_arbitrage',
                    'risk_level': risk_level,
                    'score': score,
                    'symbol': symbol,
                    'exchange': exchange,
                    'basis': basis,
                    'position_size': position_size,
                    'expected_return': profit['net_profit'],
                    'expected_return_pct': profit['net_profit_pct'],
                    'spot_price': data['spot_ask'],
                    'futures_price': data['futures_bid'],
                    'current_funding_rate': data['funding_rate'],
                    'estimated_hold_days': 3,
                    'details': profit,
                    'detected_at': datetime.now().isoformat(),
                    'status': 'pending'
                }

                opportunities.append(opportunity)

            except Exception as e:
                logger.debug(f"Error calculating basis arbitrage opportunity: {e}")

        return opportunities

    def get_opportunities(self, limit: int = None, min_score: float = None) -> List[Dict[str, Any]]:
        """
        获取机会列表
        limit: 返回数量限制
        min_score: 最小评分过滤
        """
        opps = self.opportunities

        if min_score is not None:
            opps = [opp for opp in opps if opp['score'] >= min_score]

        if limit is not None:
            opps = opps[:limit]

        return opps

    def get_top_opportunities_by_type(self, limit: int = 5) -> Dict[str, List[Dict[str, Any]]]:
        """按策略类型获取Top N机会"""
        result = {
            'funding_rate_cross_exchange': [],
            'funding_rate_spot_futures': [],
            'basis_arbitrage': []
        }

        for opp in self.opportunities:
            opp_type = opp['type']
            if opp_type in result and len(result[opp_type]) < limit:
                result[opp_type].append(opp)

        return result
