"""
Bitget交易所适配器
"""
import ccxt
from typing import Dict, Any, Optional
from loguru import logger
from .base_exchange import BaseExchange


class BitgetAdapter(BaseExchange):
    def _init_exchange(self):
        """初始化Bitget交易所"""
        self.exchange = ccxt.bitget({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'password': self.passphrase,  # Bitget需要passphrase（称为password）
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap',  # 默认使用永续合约
            }
        })

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        """
        Bitget永续合约格式: BTC/USDT:USDT
        """
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol

    def create_market_order(self, symbol: str, side: str, amount: float,
                           is_futures: bool = False, cost: Optional[float] = None,
                           reduce_only: bool = False) -> Optional[Dict[str, Any]]:
        """
        重写Bitget的市价单创建（处理对冲持仓模式）
        
        Bitget对冲持仓模式(hedge_mode)参数说明:
        - 开仓: 使用 holdSide + tradeSide='open'
        - 平仓: 使用 close_position() 方法
        
        参数说明:
        - reduce_only: True表示平仓，False表示开仓
        
        开仓参数对应关系:
        - buy + holdSide='long' + tradeSide='open' = 开多仓
        - sell + holdSide='short' + tradeSide='open' = 开空仓
        
        平仓参数对应关系:
        - close_position(side='long') = 平多仓
        - close_position(side='short') = 平空仓
        """
        try:
            if is_futures:
                symbol = self._convert_to_futures_symbol(symbol)
            
            # 平仓操作使用CCXT的close_position方法
            if reduce_only:
                logger.info(f"Closing position: symbol={symbol}, side={side}")
                # 根据side确定平仓方向
                # side='sell' 表示卖出操作，对应平多仓(long)
                # side='buy' 表示买入操作，对应平空仓(short)
                position_side = 'long' if side == 'sell' else 'short'
                order = self.exchange.close_position(
                    symbol=symbol,
                    side=position_side,  # hedge模式需要指定持仓方向
                    params={}
                )
                logger.info(f"Position closed successfully: {order.get('id')}")
                return order
            
            # 开仓操作使用create_market_order
            params = {}
            if is_futures:
                # 开仓逻辑
                if side == 'buy':
                    # 开多仓
                    params = {
                        'holdSide': 'long',   # 持仓方向：做多
                        'tradeSide': 'open',  # 交易方向：开仓
                    }
                elif side == 'sell':
                    # 开空仓
                    params = {
                        'holdSide': 'short',  # 持仓方向：做空
                        'tradeSide': 'open',  # 交易方向：开仓
                    }
            
            logger.info(f"Bitget order params: side={side}, amount={amount}, params={params}")
            
            # 使用 create_market_order
            order = self.exchange.create_market_order(
                symbol,
                side,
                amount,
                params=params
            )
            logger.info(f"Bitget market order created successfully: {order.get('id')}")
            return order
        except Exception as e:
            logger.error(f"Error creating Bitget market order: {str(e)}")
            return None
