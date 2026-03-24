"""
MyTradingPlatform — 智能订单执行层
TWAP / VWAP 算法 + 智能订单路由 + 异步并发执行

Phase 3: 订单执行层
- TWAP (时间加权平均价格)
- VWAP (成交量加权平均价格)
- 智能订单拆分与隐藏
- 滑点预估与控制
- 多交易所路由
"""

import time
import logging
import threading
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from queue import Queue, Empty

logger = logging.getLogger(__name__)


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    TWAP = "twap"
    VWAP = "vwap"
    ICEBERG = "iceberg"


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderRequest:
    id: str = ""
    symbol: str = ""
    exchange: str = ""
    side: str = ""  # buy/sell
    type: OrderType = OrderType.MARKET
    total_amount: float = 0.0
    price: float = 0.0  # 限价单使用
    leverage: int = 1
    # 算法参数
    twap_duration_sec: int = 60  # TWAP 持续时间
    twap_slices: int = 10       # TWAP 切片数
    vwap_window: int = 20       # VWAP 参考窗口
    iceberg_visible_pct: float = 10.0  # 冰山单可见比例
    # 控制参数
    max_slippage_pct: float = 0.5
    time_in_force: str = "GTC"  # GTC/IOC/FOK
    # 元数据
    strategy_id: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0


@dataclass
class OrderFill:
    order_id: str = ""
    side: str = ""
    price: float = 0.0
    amount: float = 0.0
    cost: float = 0.0
    fee: float = 0.0
    fee_currency: str = ""
    timestamp: float = 0.0
    slippage_pct: float = 0.0


@dataclass
class OrderResult:
    order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    requested_amount: float = 0.0
    filled_amount: float = 0.0
    avg_price: float = 0.0
    total_cost: float = 0.0
    total_fee: float = 0.0
    fills: List[OrderFill] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    slippage_pct: float = 0.0
    vwap_deviation: float = 0.0  # 相对 VWAP 的偏差

    @property
    def is_complete(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.FILLED)

    @property
    def duration_sec(self) -> float:
        if self.end_time and self.start_time:
            return self.end_time - self.start_time
        return 0.0


# ================================================================
# 滑点预估器
# ================================================================

class SlippageEstimator:
    """基于 OrderBook 深度预估滑点"""

    @staticmethod
    def estimate(orderbook, side: str, amount: float) -> Dict:
        """
        预估吃掉 amount 数量的滑点
        返回: {estimated_price, slippage_pct, impact_pct, levels_consumed}
        """
        if not orderbook:
            return {'slippage_pct': 0.1, 'estimated_price': 0, 'levels': 0}

        book = orderbook.bids if side == 'sell' else orderbook.asks
        if not book:
            return {'slippage_pct': 0.1, 'estimated_price': 0, 'levels': 0}

        mid = orderbook.mid_price
        if mid == 0:
            return {'slippage_pct': 0.1, 'estimated_price': 0, 'levels': 0}

        remaining = amount
        total_cost = 0.0
        levels_consumed = 0

        for lv in book:
            if remaining <= 0:
                break
            fill = min(remaining, lv.amount)
            total_cost += fill * lv.price
            remaining -= fill
            levels_consumed += 1

        if amount - remaining > 0:
            avg_price = total_cost / (amount - remaining)
            slippage = abs(avg_price - mid) / mid * 100
            impact = levels_consumed / len(book) * 100 if book else 0
            return {
                'estimated_price': round(avg_price, 8),
                'slippage_pct': round(slippage, 4),
                'impact_pct': round(impact, 2),
                'levels_consumed': levels_consumed,
                'fillable': remaining <= 0,
            }

        return {'slippage_pct': 0.5, 'estimated_price': 0, 'levels': len(book), 'fillable': False}


# ================================================================
# TWAP 执行器
# ================================================================

class TWAPExecutor:
    """
    TWAP (时间加权平均价格) 执行器
    将大额订单均匀切分为多个小订单，在指定时间段内分批执行
    """

    def __init__(self, exchange_client_factory):
        """
        exchange_client_factory: callable(exchange_id) -> ExchangeClient
        """
        self._client_factory = exchange_client_factory

    def execute(self, order: OrderRequest,
                progress_callback: Optional[callable] = None) -> OrderResult:
        """
        执行 TWAP 订单
        """
        result = OrderResult(
            order_id=order.id,
            requested_amount=order.total_amount,
            start_time=time.time(),
        )

        if order.total_amount <= 0:
            result.status = OrderStatus.FAILED
            return result

        slice_amount = order.total_amount / order.twap_slices
        slice_interval = order.twap_duration_sec / order.twap_slices

        client = self._client_factory(order.exchange)
        remaining = order.total_amount

        logger.info(f"TWAP start: {order.id} {order.side} {order.total_amount} "
                    f"over {order.twap_duration_sec}s in {order.twap_slices} slices")

        for i in range(order.twap_slices):
            if remaining <= 0:
                break

            current_slice = min(slice_amount, remaining)

            try:
                # 获取当前价格用于滑点检查
                ticker = client.fetch_ohlcv(order.symbol, '1m', limit=1)
                if ticker is not None and len(ticker) > 0:
                    current_price = float(ticker.iloc[-1]['close'])

                    # 滑点保护
                    if order.price > 0:
                        slip = abs(current_price - order.price) / order.price * 100
                        if slip > order.max_slippage_pct:
                            logger.warning(f"TWAP slice {i}: slippage {slip:.2f}% exceeds limit, skipping")
                            time.sleep(slice_interval)
                            continue

                # 执行当前切片
                if order.type == OrderType.TWAP and order.price > 0:
                    # 限价单
                    exchange_order = client.exchange.create_limit_order(
                        order.symbol, order.side, current_slice, order.price
                    )
                else:
                    # 市价单
                    exchange_order = client.create_market_order(
                        order.symbol, order.side, current_slice
                    )

                fill_price = float(exchange_order.get('price', exchange_order.get('average', 0)))
                fill_amount = float(exchange_order.get('filled', current_slice))

                fill = OrderFill(
                    order_id=order.id,
                    side=order.side,
                    price=fill_price,
                    amount=fill_amount,
                    cost=fill_price * fill_amount,
                    timestamp=time.time(),
                )
                result.fills.append(fill)
                result.filled_amount += fill_amount
                remaining -= fill_amount

                logger.info(f"TWAP slice {i+1}/{order.twap_slices}: "
                           f"filled {fill_amount:.4f} @ {fill_price:.2f}")

                if progress_callback:
                    progress_callback({
                        'slice': i + 1,
                        'total_slices': order.twap_slices,
                        'filled': result.filled_amount,
                        'remaining': remaining,
                        'progress_pct': result.filled_amount / order.total_amount * 100,
                    })

            except Exception as e:
                logger.error(f"TWAP slice {i} failed: {e}")
                # 继续下一个切片，不中断整个订单

            # 等待下一个切片间隔
            if i < order.twap_slices - 1:
                time.sleep(slice_interval)

        # 汇总结果
        result.end_time = time.time()
        if result.filled_amount > 0:
            result.avg_price = sum(f.cost for f in result.fills) / result.filled_amount
            result.total_cost = sum(f.cost for f in result.fills)
            result.total_fee = sum(f.fee for f in result.fills)

        if result.filled_amount >= order.total_amount * 0.99:
            result.status = OrderStatus.FILLED
        elif result.filled_amount > 0:
            result.status = OrderStatus.PARTIAL
        else:
            result.status = OrderStatus.FAILED

        logger.info(f"TWAP complete: {order.id} status={result.status.value} "
                    f"filled={result.filled_amount:.4f}/{order.total_amount:.4f} "
                    f"avg_price={result.avg_price:.2f}")

        return result


# ================================================================
# VWAP 执行器
# ================================================================

class VWAPExecutor:
    """
    VWAP (成交量加权平均价格) 执行器
    根据历史成交量分布，动态调整每个切片的执行量
    在成交量大的时段执行更多，成交量小时段执行更少
    """

    def __init__(self, exchange_client_factory):
        self._client_factory = exchange_client_factory

    def _get_volume_profile(self, client, symbol: str,
                            timeframe: str = '1h', lookback: int = 24) -> List[float]:
        """获取历史成交量分布"""
        try:
            df = client.fetch_ohlcv(symbol, timeframe, limit=lookback)
            if df is not None and len(df) > 0:
                volumes = df['volume'].values
                # 归一化为比例
                total = volumes.sum()
                if total > 0:
                    return (volumes / total).tolist()
        except Exception as e:
            logger.warning(f"Volume profile fetch failed: {e}")

        # fallback: 均匀分布
        n = lookback or 10
        return [1.0 / n] * n

    def execute(self, order: OrderRequest,
                progress_callback: Optional[callable] = None) -> OrderResult:
        result = OrderResult(
            order_id=order.id,
            requested_amount=order.total_amount,
            start_time=time.time(),
        )

        if order.total_amount <= 0:
            result.status = OrderStatus.FAILED
            return result

        client = self._client_factory(order.exchange)

        # 获取成交量分布
        vol_profile = self._get_volume_profile(
            client, order.symbol,
            lookback=order.vwap_window
        )

        # 将订单按成交量分布切分
        slices = []
        for ratio in vol_profile:
            slice_amt = order.total_amount * ratio
            if slice_amt > 0:
                slices.append(slice_amt)

        if not slices:
            slices = [order.total_amount / order.twap_slices] * order.twap_slices

        slice_interval = order.twap_duration_sec / len(slices)
        remaining = order.total_amount

        logger.info(f"VWAP start: {order.id} {order.side} {order.total_amount} "
                    f"over {len(slices)} volume-weighted slices")

        for i, slice_amount in enumerate(slices):
            if remaining <= 0:
                break

            current_slice = min(slice_amount, remaining)

            try:
                exchange_order = client.create_market_order(
                    order.symbol, order.side, current_slice
                )

                fill_price = float(exchange_order.get('price', exchange_order.get('average', 0)))
                fill_amount = float(exchange_order.get('filled', current_slice))

                fill = OrderFill(
                    order_id=order.id,
                    side=order.side,
                    price=fill_price,
                    amount=fill_amount,
                    cost=fill_price * fill_amount,
                    timestamp=time.time(),
                )
                result.fills.append(fill)
                result.filled_amount += fill_amount
                remaining -= fill_amount

                logger.info(f"VWAP slice {i+1}/{len(slices)}: "
                           f"filled {fill_amount:.4f} @ {fill_price:.2f}")

                if progress_callback:
                    progress_callback({
                        'slice': i + 1,
                        'total_slices': len(slices),
                        'filled': result.filled_amount,
                        'remaining': remaining,
                        'progress_pct': result.filled_amount / order.total_amount * 100,
                    })

            except Exception as e:
                logger.error(f"VWAP slice {i} failed: {e}")

            if i < len(slices) - 1:
                time.sleep(slice_interval)

        # 汇总
        result.end_time = time.time()
        if result.filled_amount > 0:
            result.avg_price = sum(f.cost for f in result.fills) / result.filled_amount
            result.total_cost = sum(f.cost for f in result.fills)

            # 计算 VWAP 偏差
            vwap = sum(f.price * f.amount for f in result.fills) / result.filled_amount
            if vwap > 0:
                result.vwap_deviation = (result.avg_price - vwap) / vwap * 100

        if result.filled_amount >= order.total_amount * 0.99:
            result.status = OrderStatus.FILLED
        elif result.filled_amount > 0:
            result.status = OrderStatus.PARTIAL
        else:
            result.status = OrderStatus.FAILED

        logger.info(f"VWAP complete: {order.id} status={result.status.value} "
                    f"filled={result.filled_amount:.4f} VWAP_dev={result.vwap_deviation:.4f}%")

        return result


# ================================================================
# 智能订单路由器
# ================================================================

class SmartOrderRouter:
    """
    智能订单路由器
    根据 OrderBook 深度、滑点预估，自动选择最优执行方式
    """

    def __init__(self, exchange_client_factory, data_manager=None):
        self._client_factory = exchange_client_factory
        self._data_manager = data_manager
        self.twap = TWAPExecutor(exchange_client_factory)
        self.vwap = VWAPExecutor(exchange_client_factory)
        self._order_history: List[OrderResult] = []
        self._lock = threading.Lock()

    def route_order(self, order: OrderRequest) -> OrderResult:
        """
        智能路由：根据订单大小和市场条件选择最优执行方式
        """
        # 获取 OrderBook 预估滑点
        slippage_info = self._estimate_impact(order)

        logger.info(f"Order routing: {order.id} {order.side} {order.total_amount} "
                    f"estimated_slippage={slippage_info.get('slippage_pct', 0):.4f}%")

        # 决策逻辑
        if order.type == OrderType.MARKET:
            # 小额市价单直接执行
            return self._execute_market(order)

        elif order.type == OrderType.TWAP:
            return self.twap.execute(order)

        elif order.type == OrderType.VWAP:
            return self.vwap.execute(order)

        elif order.type == OrderType.LIMIT:
            return self._execute_limit(order)

        else:
            # 自动选择
            impact = slippage_info.get('impact_pct', 0)
            if impact < 1.0:
                # 低冲击 → 市价单
                return self._execute_market(order)
            elif impact < 5.0:
                # 中等冲击 → TWAP
                order.type = OrderType.TWAP
                return self.twap.execute(order)
            else:
                # 高冲击 → VWAP
                order.type = OrderType.VWAP
                return self.vwap.execute(order)

    def _estimate_impact(self, order: OrderRequest) -> Dict:
        if self._data_manager:
            ob = self._data_manager.get_orderbook(order.exchange, order.symbol)
            if ob:
                return SlippageEstimator.estimate(ob, order.side, order.total_amount)
        return {'slippage_pct': 0.1}

    def _execute_market(self, order: OrderRequest) -> OrderResult:
        result = OrderResult(
            order_id=order.id,
            requested_amount=order.total_amount,
            start_time=time.time(),
        )
        try:
            client = self._client_factory(order.exchange)
            exchange_order = client.create_market_order(
                order.symbol, order.side, order.total_amount
            )
            fill_price = float(exchange_order.get('price', exchange_order.get('average', 0)))
            fill_amount = float(exchange_order.get('filled', order.total_amount))

            fill = OrderFill(
                order_id=order.id, side=order.side,
                price=fill_price, amount=fill_amount,
                cost=fill_price * fill_amount, timestamp=time.time(),
            )
            result.fills.append(fill)
            result.filled_amount = fill_amount
            result.avg_price = fill_price
            result.total_cost = fill_price * fill_amount
            result.status = OrderStatus.FILLED

            if order.price > 0:
                result.slippage_pct = abs(fill_price - order.price) / order.price * 100

        except Exception as e:
            logger.error(f"Market order failed: {e}")
            result.status = OrderStatus.FAILED

        result.end_time = time.time()

        with self._lock:
            self._order_history.append(result)

        return result

    def _execute_limit(self, order: OrderRequest) -> OrderResult:
        result = OrderResult(
            order_id=order.id,
            requested_amount=order.total_amount,
            start_time=time.time(),
        )
        try:
            client = self._client_factory(order.exchange)
            exchange_order = client.exchange.create_limit_order(
                order.symbol, order.side, order.total_amount, order.price
            )
            fill_price = float(exchange_order.get('price', order.price))
            fill_amount = float(exchange_order.get('filled', 0))

            fill = OrderFill(
                order_id=order.id, side=order.side,
                price=fill_price, amount=fill_amount,
                cost=fill_price * fill_amount, timestamp=time.time(),
            )
            result.fills.append(fill)
            result.filled_amount = fill_amount
            result.avg_price = fill_price
            result.status = (OrderStatus.FILLED if fill_amount >= order.total_amount * 0.99
                           else OrderStatus.PARTIAL if fill_amount > 0
                           else OrderStatus.PENDING)

        except Exception as e:
            logger.error(f"Limit order failed: {e}")
            result.status = OrderStatus.FAILED

        result.end_time = time.time()
        return result

    def get_stats(self) -> Dict:
        """获取执行统计"""
        with self._lock:
            orders = list(self._order_history)

        if not orders:
            return {'total_orders': 0}

        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        return {
            'total_orders': len(orders),
            'filled': len(filled),
            'fill_rate': round(len(filled) / len(orders) * 100, 1),
            'avg_duration_sec': round(np.mean([o.duration_sec for o in filled]), 2) if filled else 0,
            'avg_slippage_pct': round(np.mean([o.slippage_pct for o in filled]), 4) if filled else 0,
        }
