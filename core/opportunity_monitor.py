"""
机会监控系统
实时扫描套利机会，计算最优策略组合
"""
import time
import threading
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

    def get_opportunities(self, limit: int = None) -> List[Dict[str, Any]]:
        """获取当前机会列表"""
        if limit:
            return self.opportunities[:limit]
        return self.opportunities.copy()

    def _get_funding_frequency(self, long_data: Dict, short_data: Dict) -> int:
        """从两个交易所数据中获取资金费率频率（小时）"""
        # 尝试从next_funding_time计算间隔
        # 如果没有，默认返回8小时
        try:
            if 'next_funding_time' in long_data and long_data['next_funding_time']:
                # 假设大多数交易所是8小时，但有些是4小时
                # 可以通过查看交易所配置来确定
                # 简化版本：默认8小时
                return 8
            return 8
        except:
            return 8

    def _get_funding_frequency_single(self, data: Dict) -> int:
        """从单个交易所数据中获取资金费率频率（小时）"""
        try:
            if 'next_funding_time' in data and data['next_funding_time']:
                # 简化版本：默认8小时
                # 实际应该从交易所API或配置中读取
                return 8
            return 8
        except:
            return 8

    def _load_market_data_from_db(self, max_age_seconds: int = 60) -> Dict[str, Dict[str, Any]]:
        """从数据库加载最新的市场数据"""
        market_data = {}
        
        try:
            # 获取最近N秒内的最新价格数据
            current_time = int(time.time() * 1000)
            min_timestamp = current_time - (max_age_seconds * 1000)
            
            # 查询价格数据
            price_rows = self.db.execute_query(
                """
                SELECT exchange, symbol, timestamp,
                       spot_bid, spot_ask, spot_price,
                       futures_bid, futures_ask, futures_price,
                       maker_fee, taker_fee
                FROM market_prices
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                """,
                (min_timestamp,)
            )
            
            # 查询资金费率数据
            funding_rows = self.db.execute_query(
                """
                SELECT exchange, symbol, funding_rate, next_funding_time
                FROM funding_rates
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                """,
                (min_timestamp,)
            )
            
            # 构建市场数据结构
            for row in price_rows:
                symbol = row['symbol']
                exchange = row['exchange']
                
                if symbol not in market_data:
                    market_data[symbol] = {}
                if exchange not in market_data[symbol]:
                    market_data[symbol][exchange] = {}
                
                market_data[symbol][exchange].update({
                    'spot_bid': row['spot_bid'],
                    'spot_ask': row['spot_ask'],
                    'spot_price': row['spot_price'],
                    'futures_bid': row['futures_bid'],
                    'futures_ask': row['futures_ask'],
                    'futures_price': row['futures_price'],
                    'maker_fee': row['maker_fee'],
                    'taker_fee': row['taker_fee'],
                    'timestamp': row['timestamp']
                })
            
            # 合并资金费率数据
            for row in funding_rows:
                symbol = row['symbol']
                exchange = row['exchange']
                
                if symbol not in market_data:
                    market_data[symbol] = {}
                if exchange not in market_data[symbol]:
                    market_data[symbol][exchange] = {}
                
                market_data[symbol][exchange].update({
                    'funding_rate': row['funding_rate'],
                    'next_funding_time': row['next_funding_time']
                })
            
            logger.info(f"从数据库加载了 {len(market_data)} 个币种的市场数据")
            return market_data
            
        except Exception as e:
            logger.error(f"从数据库加载市场数据失败: {e}")
            return {}

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
        # 优先使用内存中的数据，如果没有则从数据库加载
        market_data = self.data_collector.get_market_data()
        
        if not market_data:
            logger.debug("内存中无数据，尝试从数据库加载最新价格...")
            market_data = self._load_market_data_from_db()
        
        if not market_data:
            logger.debug("No market data available")
            return

        new_opportunities = []

        # 创建字典的浅拷贝以避免迭代时字典被修改
        market_data_snapshot = dict(market_data)

        # 遍历所有交易对，让各策略自己判断数据完整性
        # 这样可以捕获跨交易所的机会（如交易所A有现货，交易所B有期货）
        for symbol, exchanges_data in market_data_snapshot.items():
            # 为每个交易对的交易所数据也创建快照
            exchanges_data_snapshot = dict(exchanges_data) if exchanges_data else {}
            
            # 策略1：跨交易所资金费率套利
            if self.config.get('strategy1', 'enabled', True):
                opps = self._calculate_cross_exchange_funding_opportunities(symbol, exchanges_data_snapshot)
                new_opportunities.extend(opps)

            # 策略2A：现货期货资金费率套利
            if self.config.get('strategy2a', 'enabled', True):
                opps = self._calculate_spot_futures_funding_opportunities(symbol, exchanges_data_snapshot)
                new_opportunities.extend(opps)

            # 策略2B：基差套利
            if self.config.get('strategy2b', 'enabled', True):
                opps = self._calculate_basis_arbitrage_opportunities(symbol, exchanges_data_snapshot)
                new_opportunities.extend(opps)

        # 更新机会列表 - 按预期收益率降序排序
        self.opportunities = sorted(new_opportunities, key=lambda x: x.get('expected_return_pct', 0), reverse=True)

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

                    # 动态获取资金费率频率（从下次结算时间计算）
                    funding_frequency_hours = self._get_funding_frequency(long_data, short_data)
                    times_per_day = 24 / funding_frequency_hours  # 8小时=3次/天, 4小时=6次/天
                    
                    # 计算年化费率差
                    annual_funding_diff = funding_diff * times_per_day * 365

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

                    # 计算评分
                    score = calculate_score(
                        profit['net_profit_pct'],
                        price_diff_pct,
                        annual_funding_diff
                    )

                    # 生成稳定ID：基于交易对+交易所+策略类型
                    stable_id = f"s1_{symbol}_{exchange_long}_{exchange_short}"

                    opportunity = {
                        'id': stable_id,
                        'type': 'funding_rate_cross_exchange',
                        'risk_level': 'low',
                        'score': score,
                        'symbol': symbol,
                        'long_exchange': exchange_long,
                        'short_exchange': exchange_short,
                        'funding_diff': funding_diff,
                        'funding_diff_annual': annual_funding_diff,
                        'funding_frequency_hours': funding_frequency_hours,
                        'times_per_day': times_per_day,
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
        
        check_count = 0
        missing_data = 0
        low_funding = 0
        high_basis = 0

        for exchange, data in exchanges_data.items():
            try:
                check_count += 1
                
                # 检查数据完整性
                required_fields = ['funding_rate', 'spot_ask', 'futures_bid', 'spot_price', 'futures_price', 'taker_fee', 'maker_fee']
                if not all(k in data for k in required_fields):
                    missing_data += 1
                    continue

                # 获取配置
                pair_config = self.config.get_pair_config(symbol, exchange, 's2a')
                min_funding_rate = pair_config.get('s2a_min_funding_rate', 0.05)  # 年化5% (原来是30%太高了)
                max_basis_deviation = pair_config.get('s2a_max_basis_deviation', 0.01)
                position_size = pair_config.get('s2a_position_size', 10000)

                # 动态获取资金费率频率
                funding_frequency_hours = self._get_funding_frequency_single(data)
                times_per_day = 24 / funding_frequency_hours
                
                # 计算年化资金费率
                annual_funding_rate = data['funding_rate'] * times_per_day * 365

                if annual_funding_rate < min_funding_rate:
                    low_funding += 1
                    continue

                # 计算基差（使用实际交易价格：期货做空价 - 现货买入价）
                basis = (data['futures_bid'] - data['spot_ask']) / data['spot_ask']

                if abs(basis) > max_basis_deviation:
                    high_basis += 1
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

                # 生成稳定ID
                stable_id = f"s2a_{symbol}_{exchange}"

                opportunity = {
                    'id': stable_id,
                    'type': 'funding_rate_spot_futures',
                    'risk_level': 'low',
                    'score': score,
                    'symbol': symbol,
                    'exchange': exchange,
                    'annual_funding_rate': annual_funding_rate,
                    'funding_frequency_hours': funding_frequency_hours,
                    'times_per_day': times_per_day,
                    'basis': basis,
                    'position_size': position_size,
                    'expected_return': profit['net_profit'],
                    'expected_return_pct': profit['net_profit_pct'],
                    'spot_price': data['spot_ask'],  # 现货价格（买入价）
                    'futures_price': data['futures_bid'],  # 期货价格（做空价）
                    'spot_entry_price': data['spot_ask'],  # 现货开仓价（买入价）
                    'futures_entry_price': data['futures_bid'],  # 期货开仓价（做空价）
                    'details': profit,
                    'detected_at': datetime.now().isoformat(),
                    'status': 'pending'
                }

                opportunities.append(opportunity)

            except Exception as e:
                logger.debug(f"Error calculating spot futures funding opportunity: {e}")
        
        # 添加统计日志
        if check_count > 0:
            logger.debug(f"策略2A {symbol}: 检查{check_count}, 数据不全{missing_data}, 资金费率过低{low_funding}, 基差过大{high_basis}, 机会{len(opportunities)}")

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
                min_basis = pair_config.get('s2b_min_basis', 0.02)  # 最小基差：2%
                position_size = pair_config.get('s2b_position_size', 8000)

                # 计算基差（使用实际交易价格：期货做空价 - 现货买入价）
                basis = (data['futures_bid'] - data['spot_ask']) / data['spot_ask']

                # 基差筛选：只有正基差（期货溢价）且足够大才考虑
                # 负基差（期货贴水）需要反向操作，不适用于此策略
                if basis < min_basis:
                    continue

                # 计算收益（假设持仓1天，3期资金费率）
                estimated_hold_periods = 3  # 1天 = 3期（每8小时一次）
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

                # 只要净收益为正就是有效机会（基差已经筛选过了）
                if profit['net_profit_pct'] <= 0:
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

                # 生成稳定ID
                stable_id = f"s2b_{symbol}_{exchange}"

                opportunity = {
                    'id': stable_id,
                    'type': 'basis_arbitrage',
                    'risk_level': risk_level,
                    'score': score,
                    'symbol': symbol,
                    'exchange': exchange,
                    'basis': basis,
                    'position_size': position_size,
                    'expected_return': profit['net_profit'],
                    'expected_return_pct': profit['net_profit_pct'],
                    'basis_income': profit['basis_income'],
                    'funding_income': profit['estimated_funding_income'],
                    'single_funding_rate_pct': data['funding_rate'],  # 单期资金费率
                    'spot_price': data['spot_ask'],  # 现货价格（买入价）
                    'futures_price': data['futures_bid'],  # 期货价格（做空价）
                    'spot_entry_price': data['spot_ask'],  # 现货开仓价（买入价）
                    'futures_entry_price': data['futures_bid'],  # 期货开仓价（做空价）
                    'current_funding_rate': data['funding_rate'],
                    'estimated_hold_days': 1,  # 1天持仓期
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
