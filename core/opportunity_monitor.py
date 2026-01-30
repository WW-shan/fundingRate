"""
机会监控系统
实时扫描套利机会，计算最优策略组合
"""
import time
import threading
from typing import Dict, List, Any, Optional
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

    def _get_funding_frequency_single(self, data: Dict, exchange: str = '') -> int:
        """从单个交易所数据中获取资金费率频率（小时）"""
        try:
            # 优先从API返回的funding_interval获取（毫秒）
            if 'funding_interval' in data and data.get('funding_interval'):
                interval_ms = data['funding_interval']
                # 转换为小时
                interval_hours = interval_ms / (1000 * 60 * 60)
                if interval_hours > 0:
                    return int(interval_hours)
            
            # 尝试从next_funding_time计算（需要两次数据才能计算间隔）
            # 这里暂不实现，因为需要存储历史数据
            
            # 根据交易所判断资金费率频率（后备方案）
            exchange_lower = exchange.lower()
            
            # 已知的资金费率周期（根据交易所文档）
            # Gate.io: 每8小时 (00:00, 08:00, 16:00 UTC)
            # OKX: 每8小时 (00:00, 08:00, 16:00 UTC)
            # Binance: 每8小时 (00:00, 08:00, 16:00 UTC)
            # Bybit: 每8小时 (00:00, 08:00, 16:00 UTC)
            # Bitget: 每8小时 (00:00, 08:00, 16:00 UTC)
            
            # 如果未来发现某些交易所是其他周期，在这里添加
            # if exchange_lower in ['某交易所']:
            #     return 4
            
            # 默认返回8小时
            return 8
        except Exception as e:
            logger.debug(f"获取资金费率频率失败: {e}")
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

            # 策略3：单边资金费率趋势策略
            if self.config.get('strategy3', 'enabled', False):
                opps = self._calculate_directional_opportunities(symbol, exchanges_data_snapshot)
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
        low_net_return = 0  # 新增:年化净收益过低

        for exchange, data in exchanges_data.items():
            try:
                check_count += 1
                
                # 检查数据完整性（字段存在且不为None）
                required_fields = ['funding_rate', 'spot_ask', 'futures_bid', 'spot_price', 'futures_price', 'taker_fee', 'maker_fee']
                if not all(k in data and data[k] is not None for k in required_fields):
                    missing_data += 1
                    continue

                # 获取配置
                pair_config = self.config.get_pair_config(symbol, exchange, 's2a')
                min_funding_rate = pair_config.get('s2a_min_funding_rate', 0.0005)  # 单次费率0.05%
                max_basis_deviation = pair_config.get('s2a_max_basis_deviation', 0.01)
                position_size = pair_config.get('s2a_position_size', 10000)

                # 动态获取资金费率频率
                funding_frequency_hours = self._get_funding_frequency_single(data, exchange)
                times_per_day = 24 / funding_frequency_hours

                # 策略2A只做正费率(做空期货收费)的情况
                # 负费率需要反向操作(做空现货+做多期货),暂不支持
                if data['funding_rate'] < min_funding_rate:
                    low_funding += 1
                    logger.debug(f"策略2A {exchange} {symbol}: 单次资金费率{data['funding_rate']:.4%} < {min_funding_rate:.4%}, 跳过")
                    continue

                # 计算基差(使用实际交易价格:期货做空价 - 现货买入价)
                basis = (data['futures_bid'] - data['spot_ask']) / data['spot_ask']

                if abs(basis) > max_basis_deviation:
                    high_basis += 1
                    logger.debug(f"策略2A {exchange} {symbol}: |基差{basis:.4%}| > {max_basis_deviation:.2%}, 跳过")
                    continue

                # 计算收益
                profit = calculate_spot_futures_funding_profit(
                    position_size=position_size,
                    funding_rate=data['funding_rate'],
                    spot_taker_fee=data['taker_fee'],
                    futures_taker_fee=data['taker_fee'],
                    spot_maker_fee=data['maker_fee'],
                    futures_maker_fee=data['maker_fee']
                )

                # 通过所有检查,生成机会
                # 计算年化资金费率用于评分和显示
                annual_funding_rate = data['funding_rate'] * times_per_day * 365
                
                # 计算评分 (使用单次资金费率)
                score = calculate_score(
                    data['funding_rate'],  # 使用单次资金费率
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
                    'funding_rate': data['funding_rate'],  # 单期资金费率
                    'annual_funding_rate': annual_funding_rate,  # 年化资金费率
                    'funding_frequency_hours': funding_frequency_hours,  # 资金费率频率（小时）
                    'times_per_day': times_per_day,  # 每天收取次数
                    'basis': basis,  # 基差
                    'position_size': position_size,
                    'expected_return': profit['net_profit'],  # 单期净收益（USDT）
                    'expected_return_pct': profit['net_profit_pct'],  # 单期净收益率
                    'spot_price': data['spot_ask'],  # 现货价格（买入价）
                    'futures_price': data['futures_bid'],  # 期货价格（做空价）
                    'spot_entry_price': data['spot_ask'],  # 现货开仓价（买入价）
                    'futures_entry_price': data['futures_bid'],  # 期货开仓价（做空价）
                    'details': profit,
                    'detected_at': datetime.now().isoformat(),
                    'status': 'pending'
                }

                logger.info(f"✅ 发现策略2A机会: {exchange} {symbol} 年化{annual_funding_rate:.2%} 单次{data['funding_rate']:.4%} 基差{basis:.4%}")
                opportunities.append(opportunity)

            except Exception as e:
                logger.error(f"❌ 策略2A {exchange} {symbol} 计算异常: {e}", exc_info=True)
        
        # 只在有机会时输出统计
        if len(opportunities) > 0:
            logger.info(f"策略2A {symbol}: 发现{len(opportunities)}个机会 (检查{check_count}个交易所)")

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

    def _calculate_directional_opportunities(
        self, symbol: str, exchanges_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """计算单边资金费率趋势机会 (Strategy 3)"""
        opportunities = []

        for exchange, data in exchanges_data.items():
            try:
                # 检查数据完整性
                required_fields = ['futures_ask', 'futures_bid', 'funding_rate', 'taker_fee', 'maker_fee']
                if not all(k in data for k in required_fields):
                    continue

                # 获取配置
                pair_config = self.config.get_pair_config(symbol, exchange, 's3')
                min_funding_rate = pair_config.get('s3_min_funding_rate', 0.0001)
                check_basis = pair_config.get('s3_check_basis', True)
                position_size = max(pair_config.get('s3_position_size', 10), 5)  # 固定金额，最小5 USDT

                funding_rate = data['funding_rate']

                # 只有费率绝对值大于阈值才考虑
                if abs(funding_rate) < min_funding_rate:
                    continue

                direction = None # 'short' or 'long'
                entry_price = 0

                # 判断方向
                if funding_rate > 0:
                    # 正费率，做空 (Short)
                    direction = 'short'
                    entry_price = data['futures_bid']

                    # 检查基差 (期货 > 现货)
                    if check_basis and 'spot_ask' in data:
                        if data['futures_bid'] <= data['spot_ask']:
                            continue

                else:
                    # 负费率，做多 (Long)
                    direction = 'long'
                    entry_price = data['futures_ask']

                    # 检查基差 (期货 < 现货)
                    if check_basis and 'spot_bid' in data:
                        if data['futures_ask'] >= data['spot_bid']:
                            continue

                if not direction:
                    continue

                # 计算预期收益 (年化)
                funding_frequency_hours = self._get_funding_frequency_single(data, exchange)
                times_per_day = 24 / funding_frequency_hours
                annual_funding_rate = abs(funding_rate) * times_per_day * 365

                # 简单估算年化净收益 (扣除开平仓手续费，假设持仓7天)
                holding_days = 7
                funding_income_pct = abs(funding_rate) * times_per_day * holding_days
                fees_pct = (data['taker_fee'] + data['maker_fee'])  # 开仓Taker，平仓Maker
                net_return_pct = funding_income_pct - fees_pct

                # 转换为年化
                annual_net_return = (net_return_pct / holding_days) * 365

                # 计算评分
                score = calculate_score(
                    annual_net_return / 365,  # 传入日化
                    0,  # 风险因子暂定为0，因为是单边
                    annual_funding_rate
                )

                stable_id = f"s3_{symbol}_{exchange}_{direction}"

                opportunity = {
                    'id': stable_id,
                    'type': 'directional_funding',  # Strategy 3
                    'risk_level': 'high',  # 单边策略风险较高
                    'score': score,
                    'symbol': symbol,
                    'exchange': exchange,
                    'direction': direction,
                    'funding_rate': funding_rate,
                    'annual_funding_rate': annual_funding_rate,
                    'funding_frequency_hours': funding_frequency_hours,  # 提升到顶层
                    'times_per_day': times_per_day,  # 提升到顶层
                    'position_size': position_size,
                    'entry_price': entry_price,
                    'expected_return': position_size * (net_return_pct / 100), # 估算
                    'expected_return_pct': net_return_pct,
                    'detected_at': datetime.now().isoformat(),
                    'status': 'pending',
                    'details': {
                        'holding_days': holding_days
                    }
                }

                opportunities.append(opportunity)

            except Exception as e:
                logger.debug(f"Error calculating directional opportunity: {e}")

        return opportunities

    def get_opportunities(self, limit: Optional[int] = None, min_score: Optional[float] = None) -> List[Dict[str, Any]]:
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
