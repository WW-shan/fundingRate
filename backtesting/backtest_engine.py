"""
回测引擎
负责历史数据回测和策略验证
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from loguru import logger
import pandas as pd


class BacktestEngine:
    """回测引擎"""

    def __init__(self, db_manager, config_manager):
        """初始化回测引擎"""
        self.db_manager = db_manager
        self.config_manager = config_manager
        self.results = []
        self.positions = []
        self.current_capital = 0
        self.initial_capital = 0

    def run_backtest(
        self,
        start_date: str,
        end_date: str,
        initial_capital: float,
        strategies: List[str],
        parameters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        运行回测

        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            initial_capital: 初始资金
            strategies: 要测试的策略列表 ['strategy1', 'strategy2a', 'strategy2b']
            parameters: 策略参数覆盖

        Returns:
            回测结果字典
        """
        logger.info(f"Starting backtest from {start_date} to {end_date}")
        logger.info(f"Initial capital: {initial_capital}, Strategies: {strategies}")

        # 重置状态
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.positions = []
        self.results = []

        try:
            # 加载历史资金费率数据
            funding_rates = self._load_funding_rates(start_date, end_date)
            if not funding_rates:
                logger.warning("No funding rate data found for the specified period")
                return self._generate_results_summary()

            # 按时间戳分组数据
            grouped_data = self._group_by_timestamp(funding_rates)

            # 逐个时间点回测
            for timestamp, rates in grouped_data.items():
                self._process_timestamp(timestamp, rates, strategies, parameters or {})

            # 生成回测报告
            summary = self._generate_results_summary()
            logger.info(f"Backtest completed. Final capital: {self.current_capital:.2f}")

            return summary

        except Exception as e:
            logger.error(f"Error during backtest: {e}")
            raise

    def _load_funding_rates(self, start_date: str, end_date: str) -> List[Dict]:
        """加载历史资金费率数据"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, exchange, symbol, funding_rate, timestamp
                    FROM funding_rates
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                """, (start_date, end_date))

                columns = [desc[0] for desc in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Error loading funding rates: {e}")
            return []

    def _group_by_timestamp(self, funding_rates: List[Dict]) -> Dict[str, List[Dict]]:
        """按时间戳分组资金费率数据"""
        grouped = {}
        for rate in funding_rates:
            timestamp = rate['timestamp']
            if timestamp not in grouped:
                grouped[timestamp] = []
            grouped[timestamp].append(rate)
        return grouped

    def _process_timestamp(
        self,
        timestamp: str,
        rates: List[Dict],
        strategies: List[str],
        parameters: Dict[str, Any]
    ):
        """处理单个时间点的数据"""
        # 更新现有持仓
        self._update_positions(timestamp, rates)

        # 查找新机会
        opportunities = self._find_opportunities(rates, strategies, parameters)

        # 执行交易
        for opp in opportunities:
            if self._can_open_position():
                self._open_position(timestamp, opp)

    def _update_positions(self, timestamp: str, rates: List[Dict]):
        """更新持仓状态并计算盈亏"""
        rates_dict = {(r['exchange'], r['symbol']): r['funding_rate'] for r in rates}

        for position in self.positions[:]:
            if position['status'] != 'open':
                continue

            # 计算资金费率收益
            long_rate = rates_dict.get((position['long_exchange'], position['symbol']), 0)
            short_rate = rates_dict.get((position['short_exchange'], position['symbol']), 0)

            funding_profit = (short_rate - long_rate) * position['size']
            position['funding_collected'] += funding_profit
            position['current_pnl'] = position['funding_collected'] - position['fees_paid']

            # 检查平仓条件
            if self._should_close_position(position, timestamp):
                self._close_position(position, timestamp)

    def _find_opportunities(
        self,
        rates: List[Dict],
        strategies: List[str],
        parameters: Dict[str, Any]
    ) -> List[Dict]:
        """查找套利机会"""
        opportunities = []

        # 策略1: 跨交易所套利
        if 'strategy1' in strategies:
            cross_exchange_opps = self._find_cross_exchange_opportunities(rates, parameters)
            opportunities.extend(cross_exchange_opps)

        # 策略2a: 现货-合约套利
        if 'strategy2a' in strategies:
            spot_futures_opps = self._find_spot_futures_opportunities(rates, parameters)
            opportunities.extend(spot_futures_opps)

        return opportunities

    def _find_cross_exchange_opportunities(
        self,
        rates: List[Dict],
        parameters: Dict[str, Any]
    ) -> List[Dict]:
        """查找跨交易所套利机会"""
        min_spread = parameters.get('min_spread', 0.0003)
        opportunities = []

        # 按symbol分组
        symbol_rates = {}
        for rate in rates:
            symbol = rate['symbol']
            if symbol not in symbol_rates:
                symbol_rates[symbol] = []
            symbol_rates[symbol].append(rate)

        # 查找spread
        for symbol, symbol_rate_list in symbol_rates.items():
            if len(symbol_rate_list) < 2:
                continue

            # 找最高和最低费率
            sorted_rates = sorted(symbol_rate_list, key=lambda x: x['funding_rate'])
            min_rate = sorted_rates[0]
            max_rate = sorted_rates[-1]

            spread = max_rate['funding_rate'] - min_rate['funding_rate']

            if spread >= min_spread:
                opportunities.append({
                    'strategy_type': 'strategy1',
                    'symbol': symbol,
                    'long_exchange': min_rate['exchange'],
                    'short_exchange': max_rate['exchange'],
                    'spread': spread,
                    'long_rate': min_rate['funding_rate'],
                    'short_rate': max_rate['funding_rate']
                })

        return opportunities

    def _find_spot_futures_opportunities(
        self,
        rates: List[Dict],
        parameters: Dict[str, Any]
    ) -> List[Dict]:
        """查找现货-合约套利机会"""
        min_rate = parameters.get('min_funding_rate', 0.0005)
        opportunities = []

        for rate in rates:
            if rate['funding_rate'] >= min_rate:
                opportunities.append({
                    'strategy_type': 'strategy2a',
                    'symbol': rate['symbol'],
                    'exchange': rate['exchange'],
                    'funding_rate': rate['funding_rate']
                })

        return opportunities

    def _can_open_position(self) -> bool:
        """检查是否可以开新仓"""
        max_positions = self.config_manager.get('global', 'max_positions', 10)
        open_positions = len([p for p in self.positions if p['status'] == 'open'])
        return open_positions < max_positions and self.current_capital > 0

    def _open_position(self, timestamp: str, opportunity: Dict):
        """开仓"""
        position_size = self.config_manager.get('global', 'position_size', 1000)
        position_size = min(position_size, self.current_capital * 0.1)  # 最多使用10%资金

        # 计算手续费 (假设0.05%)
        fee_rate = 0.0005
        fees = position_size * fee_rate * 2  # 开仓两边

        position = {
            'id': len(self.positions) + 1,
            'strategy_type': opportunity['strategy_type'],
            'symbol': opportunity['symbol'],
            'size': position_size,
            'open_time': timestamp,
            'close_time': None,
            'status': 'open',
            'fees_paid': fees,
            'funding_collected': 0,
            'current_pnl': -fees,
            'realized_pnl': 0
        }

        if opportunity['strategy_type'] == 'strategy1':
            position['long_exchange'] = opportunity['long_exchange']
            position['short_exchange'] = opportunity['short_exchange']
            position['entry_spread'] = opportunity['spread']
        else:
            position['exchange'] = opportunity.get('exchange')
            position['entry_funding_rate'] = opportunity.get('funding_rate', 0)

        self.positions.append(position)
        self.current_capital -= fees

        logger.debug(f"Opened position: {position['strategy_type']} {position['symbol']} @ {timestamp}")

    def _should_close_position(self, position: Dict, timestamp: str) -> bool:
        """判断是否应该平仓"""
        # 简单策略: 持仓超过7天或PnL达到目标
        open_time = datetime.fromisoformat(position['open_time'])
        current_time = datetime.fromisoformat(timestamp)
        days_held = (current_time - open_time).days

        # 持仓超过7天
        if days_held >= 7:
            return True

        # 盈利达到目标
        target_profit = position['size'] * 0.01  # 目标1%
        if position['current_pnl'] >= target_profit:
            return True

        # 止损
        stop_loss = position['size'] * -0.005  # 止损-0.5%
        if position['current_pnl'] <= stop_loss:
            return True

        return False

    def _close_position(self, position: Dict, timestamp: str):
        """平仓"""
        fee_rate = 0.0005
        close_fees = position['size'] * fee_rate * 2

        position['status'] = 'closed'
        position['close_time'] = timestamp
        position['fees_paid'] += close_fees
        position['realized_pnl'] = position['current_pnl'] - close_fees

        self.current_capital += position['size'] + position['realized_pnl']

        self.results.append(position.copy())

        logger.debug(
            f"Closed position: {position['strategy_type']} {position['symbol']} "
            f"PnL: {position['realized_pnl']:.2f}"
        )

    def _generate_results_summary(self) -> Dict[str, Any]:
        """生成回测结果摘要"""
        if not self.results:
            return {
                'total_trades': 0,
                'profitable_trades': 0,
                'losing_trades': 0,
                'total_pnl': 0,
                'total_fees': 0,
                'win_rate': 0,
                'roi': 0,
                'final_capital': self.current_capital,
                'trades': []
            }

        total_pnl = sum(r['realized_pnl'] for r in self.results)
        total_fees = sum(r['fees_paid'] for r in self.results)
        profitable = len([r for r in self.results if r['realized_pnl'] > 0])
        losing = len([r for r in self.results if r['realized_pnl'] <= 0])

        return {
            'total_trades': len(self.results),
            'profitable_trades': profitable,
            'losing_trades': losing,
            'total_pnl': round(total_pnl, 2),
            'total_fees': round(total_fees, 2),
            'win_rate': round(profitable / len(self.results) * 100, 2) if self.results else 0,
            'roi': round((self.current_capital - self.initial_capital) / self.initial_capital * 100, 2),
            'initial_capital': self.initial_capital,
            'final_capital': round(self.current_capital, 2),
            'max_drawdown': self._calculate_max_drawdown(),
            'trades': self.results[:100]  # 返回前100个交易
        }

    def _calculate_max_drawdown(self) -> float:
        """计算最大回撤"""
        if not self.results:
            return 0

        peak = self.initial_capital
        max_dd = 0
        current = self.initial_capital

        for result in self.results:
            current += result['realized_pnl']
            if current > peak:
                peak = current
            dd = (peak - current) / peak * 100
            if dd > max_dd:
                max_dd = dd

        return round(max_dd, 2)

    def save_backtest_results(self, results: Dict[str, Any], name: str):
        """保存回测结果到数据库"""
        try:
            with self.db_manager.get_connection() as conn:
                cursor = conn.cursor()

                # 创建回测结果表(如果不存在)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS backtest_results (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        initial_capital REAL,
                        final_capital REAL,
                        total_trades INTEGER,
                        win_rate REAL,
                        roi REAL,
                        max_drawdown REAL,
                        results_json TEXT
                    )
                """)

                # 插入结果
                import json
                cursor.execute("""
                    INSERT INTO backtest_results
                    (name, timestamp, initial_capital, final_capital, total_trades, win_rate, roi, max_drawdown, results_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    name,
                    datetime.now().isoformat(),
                    results['initial_capital'],
                    results['final_capital'],
                    results['total_trades'],
                    results['win_rate'],
                    results['roi'],
                    results['max_drawdown'],
                    json.dumps(results)
                ))

                conn.commit()
                logger.info(f"Saved backtest results: {name}")

        except Exception as e:
            logger.error(f"Error saving backtest results: {e}")
