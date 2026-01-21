"""
回测结果分析和可视化
"""

from typing import Dict, List, Any
import matplotlib
matplotlib.use('Agg')  # 非GUI后端
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime
from loguru import logger
import os


class ResultsAnalyzer:
    """回测结果分析器"""

    def __init__(self):
        """初始化分析器"""
        sns.set_style('whitegrid')
        plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans']
        plt.rcParams['axes.unicode_minus'] = False

    def generate_report(self, results: Dict[str, Any], output_path: str = None) -> str:
        """
        生成回测报告

        Args:
            results: 回测结果字典
            output_path: 输出路径，None则使用默认路径

        Returns:
            报告文本
        """
        report_lines = []
        report_lines.append("=" * 60)
        report_lines.append("回测报告")
        report_lines.append("=" * 60)
        report_lines.append("")

        # 基本信息
        report_lines.append("## 基本信息")
        report_lines.append(f"初始资金: {results.get('initial_capital', 0):.2f} USDT")
        report_lines.append(f"最终资金: {results.get('final_capital', 0):.2f} USDT")
        report_lines.append(f"总盈亏: {results.get('total_pnl', 0):.2f} USDT")
        report_lines.append(f"投资回报率: {results.get('roi', 0):.2f}%")
        report_lines.append(f"最大回撤: {results.get('max_drawdown', 0):.2f}%")
        report_lines.append("")

        # 交易统计
        report_lines.append("## 交易统计")
        report_lines.append(f"总交易次数: {results.get('total_trades', 0)}")
        report_lines.append(f"盈利交易: {results.get('profitable_trades', 0)}")
        report_lines.append(f"亏损交易: {results.get('losing_trades', 0)}")
        report_lines.append(f"胜率: {results.get('win_rate', 0):.2f}%")
        report_lines.append(f"总手续费: {results.get('total_fees', 0):.2f} USDT")
        report_lines.append("")

        # 策略分析
        if results.get('trades'):
            report_lines.append("## 策略分析")
            strategy_stats = self._analyze_by_strategy(results['trades'])
            for strategy, stats in strategy_stats.items():
                report_lines.append(f"\n### {strategy}")
                report_lines.append(f"  交易次数: {stats['count']}")
                report_lines.append(f"  总盈亏: {stats['total_pnl']:.2f} USDT")
                report_lines.append(f"  平均盈亏: {stats['avg_pnl']:.2f} USDT")
                report_lines.append(f"  胜率: {stats['win_rate']:.2f}%")

        report_lines.append("")
        report_lines.append("=" * 60)

        report_text = "\n".join(report_lines)

        # 保存报告
        if output_path:
            try:
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(report_text)
                logger.info(f"Report saved to: {output_path}")
            except Exception as e:
                logger.error(f"Error saving report: {e}")

        return report_text

    def _analyze_by_strategy(self, trades: List[Dict]) -> Dict[str, Dict]:
        """按策略分析交易"""
        strategy_stats = {}

        for trade in trades:
            strategy = trade.get('strategy_type', 'unknown')
            if strategy not in strategy_stats:
                strategy_stats[strategy] = {
                    'count': 0,
                    'total_pnl': 0,
                    'profitable': 0
                }

            stats = strategy_stats[strategy]
            stats['count'] += 1
            stats['total_pnl'] += trade.get('realized_pnl', 0)
            if trade.get('realized_pnl', 0) > 0:
                stats['profitable'] += 1

        # 计算平均值和胜率
        for strategy, stats in strategy_stats.items():
            stats['avg_pnl'] = stats['total_pnl'] / stats['count'] if stats['count'] > 0 else 0
            stats['win_rate'] = stats['profitable'] / stats['count'] * 100 if stats['count'] > 0 else 0

        return strategy_stats

    def plot_equity_curve(self, results: Dict[str, Any], output_path: str):
        """绘制权益曲线"""
        try:
            trades = results.get('trades', [])
            if not trades:
                logger.warning("No trades to plot")
                return

            # 计算累计权益
            initial_capital = results.get('initial_capital', 100000)
            equity = [initial_capital]
            for trade in trades:
                equity.append(equity[-1] + trade.get('realized_pnl', 0))

            # 绘图
            plt.figure(figsize=(12, 6))
            plt.plot(equity, linewidth=2, color='#667eea')
            plt.axhline(y=initial_capital, color='gray', linestyle='--', alpha=0.5)
            plt.title('权益曲线', fontsize=16, fontweight='bold')
            plt.xlabel('交易次数', fontsize=12)
            plt.ylabel('权益 (USDT)', fontsize=12)
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(output_path, dpi=150, bbox_inches='tight')
            plt.close()

            logger.info(f"Equity curve saved to: {output_path}")

        except Exception as e:
            logger.error(f"Error plotting equity curve: {e}")

    def plot_pnl_distribution(self, results: Dict[str, Any], output_path: str):
        """绘制盈亏分布"""
        try:
            trades = results.get('trades', [])
            if not trades:
                logger.warning("No trades to plot")
                return

            pnls = [trade.get('realized_pnl', 0) for trade in trades]

            plt.figure(figsize=(10, 6))
            plt.hist(pnls, bins=30, color='#667eea', alpha=0.7, edgecolor='black')
            plt.axvline(x=0, color='red', linestyle='--', linewidth=2)
            plt.title('盈亏分布', fontsize=16, fontweight='bold')
            plt.xlabel('盈亏 (USDT)', fontsize=12)
            plt.ylabel('交易次数', fontsize=12)
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(output_path, dpi=150, bbox_inches='tight')
            plt.close()

            logger.info(f"PnL distribution saved to: {output_path}")

        except Exception as e:
            logger.error(f"Error plotting PnL distribution: {e}")

    def plot_strategy_comparison(self, results: Dict[str, Any], output_path: str):
        """绘制策略对比"""
        try:
            trades = results.get('trades', [])
            if not trades:
                logger.warning("No trades to plot")
                return

            strategy_stats = self._analyze_by_strategy(trades)

            strategies = list(strategy_stats.keys())
            pnls = [stats['total_pnl'] for stats in strategy_stats.values()]
            win_rates = [stats['win_rate'] for stats in strategy_stats.values()]

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

            # 总盈亏对比
            ax1.bar(strategies, pnls, color='#667eea', alpha=0.7)
            ax1.set_title('策略总盈亏对比', fontsize=14, fontweight='bold')
            ax1.set_ylabel('总盈亏 (USDT)', fontsize=12)
            ax1.grid(True, alpha=0.3, axis='y')

            # 胜率对比
            ax2.bar(strategies, win_rates, color='#764ba2', alpha=0.7)
            ax2.set_title('策略胜率对比', fontsize=14, fontweight='bold')
            ax2.set_ylabel('胜率 (%)', fontsize=12)
            ax2.set_ylim(0, 100)
            ax2.grid(True, alpha=0.3, axis='y')

            plt.tight_layout()
            plt.savefig(output_path, dpi=150, bbox_inches='tight')
            plt.close()

            logger.info(f"Strategy comparison saved to: {output_path}")

        except Exception as e:
            logger.error(f"Error plotting strategy comparison: {e}")

    def generate_all_charts(self, results: Dict[str, Any], output_dir: str = 'data/backtest_charts'):
        """生成所有图表"""
        try:
            os.makedirs(output_dir, exist_ok=True)

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

            self.plot_equity_curve(results, os.path.join(output_dir, f'equity_{timestamp}.png'))
            self.plot_pnl_distribution(results, os.path.join(output_dir, f'pnl_dist_{timestamp}.png'))
            self.plot_strategy_comparison(results, os.path.join(output_dir, f'strategy_{timestamp}.png'))

            logger.info(f"All charts generated in: {output_dir}")

        except Exception as e:
            logger.error(f"Error generating charts: {e}")
