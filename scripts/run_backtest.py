#!/usr/bin/env python3
"""
回测命令行工具
用于命令行运行回测
"""

import argparse
import sys
from datetime import datetime, timedelta
from loguru import logger

# 添加项目根目录到路径
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db_manager import DatabaseManager
from config.config_manager import ConfigManager
from backtesting import BacktestEngine, DataLoader, ResultsAnalyzer


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='资金费率套利回测工具')

    parser.add_argument('--start', required=True, help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end', required=True, help='结束日期 (YYYY-MM-DD)')
    parser.add_argument('--capital', type=float, default=100000, help='初始资金 (默认: 100000)')
    parser.add_argument('--strategies', nargs='+', default=['strategy1', 'strategy2a'],
                       help='策略列表 (默认: strategy1 strategy2a)')
    parser.add_argument('--name', help='回测名称')
    parser.add_argument('--save', action='store_true', help='保存结果到数据库')
    parser.add_argument('--report', action='store_true', help='生成报告')
    parser.add_argument('--charts', action='store_true', help='生成图表')

    args = parser.parse_args()

    # 初始化组件
    logger.info("Initializing components...")
    db_manager = DatabaseManager()
    config_manager = ConfigManager()

    # 创建回测引擎
    backtest_engine = BacktestEngine(db_manager, config_manager)
    data_loader = DataLoader(db_manager)
    analyzer = ResultsAnalyzer()

    # 检查数据可用性
    date_range = data_loader.get_available_date_range()
    if not date_range['start_date']:
        logger.error("No funding rate data available. Please collect data first.")
        return

    logger.info(f"Available data range: {date_range['start_date']} to {date_range['end_date']}")

    # 运行回测
    logger.info(f"Running backtest from {args.start} to {args.end}")
    logger.info(f"Initial capital: {args.capital} USDT")
    logger.info(f"Strategies: {args.strategies}")

    results = backtest_engine.run_backtest(
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        strategies=args.strategies
    )

    # 显示结果
    print("\n" + "=" * 60)
    print("回测结果")
    print("=" * 60)
    print(f"初始资金: {results['initial_capital']:.2f} USDT")
    print(f"最终资金: {results['final_capital']:.2f} USDT")
    print(f"总盈亏: {results['total_pnl']:.2f} USDT")
    print(f"投资回报率: {results['roi']:.2f}%")
    print(f"最大回撤: {results['max_drawdown']:.2f}%")
    print(f"\n交易统计:")
    print(f"  总交易: {results['total_trades']}")
    print(f"  盈利交易: {results['profitable_trades']}")
    print(f"  亏损交易: {results['losing_trades']}")
    print(f"  胜率: {results['win_rate']:.2f}%")
    print(f"  总手续费: {results['total_fees']:.2f} USDT")
    print("=" * 60)

    # 保存结果
    if args.save:
        name = args.name or f"backtest_{args.start}_{args.end}"
        backtest_engine.save_backtest_results(results, name)
        logger.info(f"Results saved as: {name}")

    # 生成报告
    if args.report:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        report_path = f"data/backtest_reports/report_{timestamp}.txt"
        os.makedirs('data/backtest_reports', exist_ok=True)
        report = analyzer.generate_report(results, report_path)
        logger.info(f"Report generated: {report_path}")

    # 生成图表
    if args.charts:
        analyzer.generate_all_charts(results)
        logger.info("Charts generated in data/backtest_charts/")


if __name__ == '__main__':
    main()
