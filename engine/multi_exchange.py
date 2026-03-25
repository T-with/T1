"""
MyTradingPlatform — 多交易所并行执行引擎
跨交易所智能路由 + asyncio并发 + 健康监控 + 最优价格发现

Phase 2 核心升级:
- 多交易所并行下单（跨所套利支持）
- asyncio 异步并发执行
- 交易所健康监控 + 自动降级
- 最优价格发现（自动选所）
- 跨交易所仓位同步
"""

import asyncio
import time
import logging
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict
import ccxt.async_support as ccxt_async

logger = logging.getLogger(__name__)


# ================================================================
# 交易所健康状态
# ================================================================

class ExchangeHealth(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class ExchangeStatus:
    exchange_id: str = ""
    health: ExchangeHealth = ExchangeHealth.UNKNOWN
    latency_ms: float = 0.0
    last_success: float = 0.0
    last_failure: float = 0.0
    failure_count: int = 0
    success_count: int = 0
    consecutive_failures: int = 0
    error_message: str = ""
    # 费率
    maker_fee: float = 0.001
    taker_fee: float = 0.001
    # 支持的交易对
    symbols: List[str] = field(default_factory=list)

    @property
    def is_available(self) -> bool:
        return self.health in (ExchangeHealth.HEALTHY, ExchangeHealth.DEGRADED)

    @property
    def uptime_pct(self) -> float:
        total = self.success_count + self.failure_count
        if total == 0:
            return 100.0
        return self.success_count / total * 100

    @property
    def score(self) -> float:
        """综合评分（0-100）: 健康度 + 延迟 + 成功率"""
        if not self.is_available:
            return 0.0
        health_score = 80 if self.health == ExchangeHealth.HEALTHY else 40
        latency_score = max(0, 20 - self.latency_ms / 50)  # <1000ms 满分
        return min(100, health_score + latency_score)


class ExchangeHealthMonitor:
    """
    交易所健康监控器
    定期探活 + 延迟测量 + 自动降级/恢复
    """

    def __init__(self, check_interval: float = 30.0):
        self.check_interval = check_interval
        self._exchanges: Dict[str, ccxt_async.Exchange] = {}
        self._status: Dict[str, ExchangeStatus] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def add_exchange(self, exchange_id: str,
                            api_key: str = '', api_secret: str = '',
                            passphrase: str = ''):
        """添加交易所到监控"""
        if exchange_id in self._exchanges:
            return

        cls = getattr(ccxt_async, exchange_id)
        params = {'enableRateLimit': True, 'timeout': 10000}
        if api_key:
            params['apiKey'] = api_key
            params['secret'] = api_secret
            if passphrase:
                params['password'] = passphrase

        exchange = cls(params)
        self._exchanges[exchange_id] = exchange
        self._status[exchange_id] = ExchangeStatus(exchange_id=exchange_id)

        # 初始探测
        await self._probe(exchange_id)

        logger.info(f"Exchange added to monitor: {exchange_id}")

    async def _probe(self, exchange_id: str):
        """探活：获取 BTC/USDT ticker 并测量延迟"""
        status = self._status.get(exchange_id)
        if not status:
            return

        exchange = self._exchanges[exchange_id]
        start = time.time()

        try:
            # 尝试获取支持的交易对
            ticker = await exchange.fetch_ticker('BTC/USDT')
            latency = (time.time() - start) * 1000

            status.latency_ms = latency
            status.last_success = time.time()
            status.success_count += 1
            status.consecutive_failures = 0
            status.error_message = ""

            # 健康判定
            if latency < 500:
                status.health = ExchangeHealth.HEALTHY
            elif latency < 2000:
                status.health = ExchangeHealth.DEGRADED
            else:
                status.health = ExchangeHealth.DEGRADED

            # 获取费率
            try:
                if hasattr(exchange, 'fees'):
                    status.maker_fee = exchange.fees.get('trading', {}).get('maker', 0.001)
                    status.taker_fee = exchange.fees.get('trading', {}).get('taker', 0.001)
            except Exception:
                pass

        except Exception as e:
            latency = (time.time() - start) * 1000
            status.latency_ms = latency
            status.last_failure = time.time()
            status.failure_count += 1
            status.consecutive_failures += 1
            status.error_message = str(e)[:200]

            if status.consecutive_failures >= 3:
                status.health = ExchangeHealth.DOWN
            elif status.consecutive_failures >= 1:
                status.health = ExchangeHealth.DEGRADED

            logger.warning(f"Probe failed [{exchange_id}]: {e}")

    async def _monitor_loop(self):
        """后台监控循环"""
        while self._running:
            tasks = [self._probe(eid) for eid in self._exchanges]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(self.check_interval)

    def start(self):
        """启动后台监控"""
        if not self._running:
            self._running = True
            try:
                loop = asyncio.get_event_loop()
                self._task = loop.create_task(self._monitor_loop())
            except RuntimeError:
                pass

    async def stop(self):
        """停止监控并关闭所有交易所连接"""
        self._running = False
        if self._task:
            self._task.cancel()
        for exchange in self._exchanges.values():
            try:
                await exchange.close()
            except Exception:
                pass

    def get_status(self, exchange_id: str = None) -> Dict:
        if exchange_id:
            s = self._status.get(exchange_id)
            return {
                'health': s.health.value if s else 'unknown',
                'latency_ms': round(s.latency_ms, 1) if s else 0,
                'uptime_pct': round(s.uptime_pct, 1) if s else 0,
                'score': round(s.score, 1) if s else 0,
                'consecutive_failures': s.consecutive_failures if s else 0,
                'error': s.error_message if s else '',
            }
        return {eid: self.get_status(eid) for eid in self._status}

    def get_best_exchange(self, symbol: str,
                           prefer_low_fee: bool = False) -> Optional[str]:
        """选择最优交易所"""
        candidates = []
        for eid, status in self._status.items():
            if not status.is_available:
                continue
            score = status.score
            if prefer_low_fee:
                score += (0.002 - status.taker_fee) * 5000  # 低费率加分
            candidates.append((eid, score))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def get_exchange(self, exchange_id: str):
        return self._exchanges.get(exchange_id)


# ================================================================
# 最优价格发现
# ================================================================

class PriceAggregator:
    """
    多交易所价格聚合
    实时获取所有交易所的买卖价，找最优
    """

    def __init__(self, monitor: ExchangeHealthMonitor):
        self._monitor = monitor
        self._prices: Dict[str, Dict[str, float]] = {}  # symbol -> {exchange: price}
        self._lock = asyncio.Lock()

    async def get_best_price(self, symbol: str, side: str) -> Tuple[Optional[str], float]:
        """
        获取最优价格的交易所

        Returns:
            (exchange_id, price)
        """
        tasks = {}
        for eid, exchange in self._monitor._exchanges.items():
            status = self._monitor._status.get(eid)
            if status and status.is_available:
                tasks[eid] = self._fetch_price(exchange, symbol)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        best_exchange = None
        best_price = None

        for eid, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                continue
            if result is None:
                continue

            bid, ask = result
            price = bid if side == 'sell' else ask  # 卖出看 bid，买入看 ask

            if best_price is None:
                best_price = price
                best_exchange = eid
            elif side == 'buy' and price < best_price:  # 买入价越低越好
                best_price = price
                best_exchange = eid
            elif side == 'sell' and price > best_price:  # 卖出价越高越好
                best_price = price
                best_exchange = eid

        return best_exchange, best_price or 0.0

    async def get_all_prices(self, symbol: str) -> Dict[str, Dict]:
        """获取所有交易所的价格"""
        tasks = {}
        for eid, exchange in self._monitor._exchanges.items():
            status = self._monitor._status.get(eid)
            if status and status.is_available:
                tasks[eid] = self._fetch_price(exchange, symbol)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        prices = {}
        for eid, result in zip(tasks.keys(), results):
            if isinstance(result, Exception) or result is None:
                continue
            bid, ask = result
            prices[eid] = {
                'bid': bid, 'ask': ask,
                'mid': (bid + ask) / 2,
                'spread': ask - bid,
            }

        return prices

    async def find_arbitrage(self, symbol: str,
                              min_spread_pct: float = 0.1) -> Optional[Dict]:
        """
        跨交易所套利机会发现
        在A所买 + 在B所卖，如果价差 > 手续费则有利润
        """
        prices = await self.get_all_prices(symbol)
        if len(prices) < 2:
            return None

        exchanges = list(prices.keys())
        best_opportunity = None
        best_profit_pct = 0

        for i, eid_a in enumerate(exchanges):
            for eid_b in exchanges[i+1:]:
                # A买B卖
                buy_price = prices[eid_a]['ask']
                sell_price = prices[eid_b]['bid']
                spread_pct = (sell_price / buy_price - 1) * 100

                # 减去双边手续费
                fee_a = self._monitor._status.get(eid_a)
                fee_b = self._monitor._status.get(eid_b)
                total_fee_pct = ((fee_a.taker_fee if fee_a else 0.001) +
                                (fee_b.taker_fee if fee_b else 0.001)) * 100

                net_profit = spread_pct - total_fee_pct

                if net_profit > best_profit_pct:
                    best_profit_pct = net_profit
                    best_opportunity = {
                        'symbol': symbol,
                        'buy_exchange': eid_a,
                        'buy_price': buy_price,
                        'sell_exchange': eid_b,
                        'sell_price': sell_price,
                        'spread_pct': round(spread_pct, 4),
                        'fee_pct': round(total_fee_pct, 4),
                        'net_profit_pct': round(net_profit, 4),
                    }

                # B买A卖
                buy_price = prices[eid_b]['ask']
                sell_price = prices[eid_a]['bid']
                spread_pct = (sell_price / buy_price - 1) * 100
                net_profit = spread_pct - total_fee_pct

                if net_profit > best_profit_pct:
                    best_profit_pct = net_profit
                    best_opportunity = {
                        'symbol': symbol,
                        'buy_exchange': eid_b,
                        'buy_price': buy_price,
                        'sell_exchange': eid_a,
                        'sell_price': sell_price,
                        'spread_pct': round(spread_pct, 4),
                        'fee_pct': round(total_fee_pct, 4),
                        'net_profit_pct': round(net_profit, 4),
                    }

        if best_opportunity and best_profit_pct >= min_spread_pct:
            return best_opportunity
        return None

    async def _fetch_price(self, exchange, symbol: str):
        try:
            ticker = await exchange.fetch_ticker(symbol)
            return float(ticker['bid']), float(ticker['ask'])
        except Exception:
            return None


# ================================================================
# 多交易所并行执行器
# ================================================================

class MultiExchangeExecutor:
    """
    多交易所并行订单执行器

    支持模式:
    1. 单所执行 — 自动选最优交易所
    2. 并行执行 — 同一订单拆分到多个交易所同时执行
    3. 跨所套利 — 在A所买入同时在B所卖出
    4. 聚合执行 — 大单拆分到多个流动性最好的交易所
    """

    def __init__(self, monitor: ExchangeHealthMonitor):
        self._monitor = monitor
        self._price_agg = PriceAggregator(monitor)
        self._order_history: List[Dict] = []

    async def execute_single(self, symbol: str, side: str, amount: float,
                              exchange_id: str = None,
                              order_type: str = 'market',
                              price: float = 0,
                              max_slippage_pct: float = 0.5) -> Dict:
        """
        单交易所执行

        如果不指定 exchange_id，自动选择最优交易所
        """
        if not exchange_id:
            exchange_id = self._monitor.get_best_exchange(symbol)
            if not exchange_id:
                return {'status': 'failed', 'error': 'No available exchange'}

        exchange = self._monitor.get_exchange(exchange_id)
        if not exchange:
            return {'status': 'failed', 'error': f'Exchange {exchange_id} not found'}

        start = time.time()
        try:
            if order_type == 'market':
                order = await exchange.create_market_order(symbol, side, amount)
            elif order_type == 'limit' and price > 0:
                order = await exchange.create_limit_order(symbol, side, amount, price)
            else:
                return {'status': 'failed', 'error': 'Invalid order type or missing price'}

            fill_price = float(order.get('price', order.get('average', 0)))
            fill_amount = float(order.get('filled', amount))
            latency = (time.time() - start) * 1000

            result = {
                'status': 'filled',
                'exchange': exchange_id,
                'symbol': symbol,
                'side': side,
                'price': fill_price,
                'amount': fill_amount,
                'cost': fill_price * fill_amount,
                'latency_ms': round(latency, 1),
                'order_id': order.get('id', ''),
            }

            self._order_history.append(result)
            logger.info(f"Order filled: {side} {fill_amount:.6f} {symbol} @ {fill_price:.2f} "
                        f"on {exchange_id} ({latency:.0f}ms)")
            return result

        except Exception as e:
            latency = (time.time() - start) * 1000
            logger.error(f"Order failed on {exchange_id}: {e}")
            return {
                'status': 'failed',
                'exchange': exchange_id,
                'error': str(e),
                'latency_ms': round(latency, 1),
            }

    async def execute_parallel(self, symbol: str, side: str,
                                total_amount: float,
                                exchanges: List[str] = None,
                                split_method: str = 'equal') -> List[Dict]:
        """
        并行执行 — 将订单拆分到多个交易所同时执行

        split_method:
        - 'equal': 均分
        - 'liquidity': 按各所流动性分配
        - 'best_price': 按最优价格分配（只选最好的1-2个）
        """
        if exchanges is None:
            # 选择所有可用交易所
            exchanges = [
                eid for eid, s in self._monitor._status.items()
                if s.is_available
            ]

        if not exchanges:
            return [{'status': 'failed', 'error': 'No available exchanges'}]

        if split_method == 'equal':
            per_exchange = total_amount / len(exchanges)
            amounts = [per_exchange] * len(exchanges)

        elif split_method == 'best_price':
            # 只选价格最优的
            best_ex, best_price = await self._price_agg.get_best_price(symbol, side)
            if best_ex:
                return [await self.execute_single(symbol, side, total_amount, best_ex)]
            else:
                return [await self.execute_single(symbol, side, total_amount, exchanges[0])]

        else:  # liquidity
            # 简化: 按延迟反比分配（延迟越低分配越多）
            latencies = []
            for eid in exchanges:
                status = self._monitor._status.get(eid)
                latencies.append(max(1, status.latency_ms if status else 1000))
            inv_lat = [1.0 / l for l in latencies]
            total_inv = sum(inv_lat)
            amounts = [total_amount * (il / total_inv) for il in inv_lat]

        # 并行执行
        tasks = [
            self.execute_single(symbol, side, amt, eid)
            for eid, amt in zip(exchanges, amounts)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 汇总
        output = []
        for r in results:
            if isinstance(r, Exception):
                output.append({'status': 'failed', 'error': str(r)})
            else:
                output.append(r)

        filled = sum(r.get('amount', 0) for r in output if r.get('status') == 'filled')
        logger.info(f"Parallel execution: {side} {filled:.6f}/{total_amount:.6f} {symbol} "
                    f"across {len(exchanges)} exchanges")
        return output

    async def execute_arbitrage(self, symbol: str, amount: float,
                                 min_profit_pct: float = 0.05) -> Dict:
        """
        跨交易所套利执行
        同时在两个交易所下单（买+卖）
        """
        opportunity = await self._price_agg.find_arbitrage(symbol, min_profit_pct)
        if not opportunity:
            return {'status': 'no_opportunity', 'symbol': symbol}

        buy_exchange = opportunity['buy_exchange']
        sell_exchange = opportunity['sell_exchange']

        logger.info(f"Executing arb: buy {amount} {symbol} on {buy_exchange} "
                    f"and sell on {sell_exchange}, "
                    f"expected profit: {opportunity['net_profit_pct']:.3f}%")

        # 同时下单（关键：时间差会导致滑点）
        buy_task = self.execute_single(symbol, 'buy', amount, buy_exchange)
        sell_task = self.execute_single(symbol, 'sell', amount, sell_exchange)

        (buy_result, sell_result) = await asyncio.gather(
            buy_task, sell_task, return_exceptions=True
        )

        result = {
            'status': 'executed',
            'symbol': symbol,
            'opportunity': opportunity,
            'buy_result': buy_result if not isinstance(buy_result, Exception)
                         else {'status': 'failed', 'error': str(buy_result)},
            'sell_result': sell_result if not isinstance(sell_result, Exception)
                          else {'status': 'failed', 'error': str(sell_result)},
        }

        # 计算实际利润
        if (isinstance(buy_result, dict) and buy_result.get('status') == 'filled' and
            isinstance(sell_result, dict) and sell_result.get('status') == 'filled'):
            buy_cost = buy_result.get('cost', 0)
            sell_revenue = sell_result.get('cost', 0)
            result['actual_profit'] = round(sell_revenue - buy_cost, 2)
            result['actual_profit_pct'] = round(
                (sell_revenue / buy_cost - 1) * 100, 4
            ) if buy_cost > 0 else 0

        return result

    async def execute_twap_multi(self, symbol: str, side: str,
                                  total_amount: float,
                                  duration_sec: int = 60,
                                  slices: int = 10) -> Dict:
        """
        多交易所 TWAP — 在多个交易所上同时执行时间加权订单
        """
        slice_amount = total_amount / slices
        interval = duration_sec / slices

        exchanges = [
            eid for eid, s in self._monitor._status.items()
            if s.is_available
        ]
        if not exchanges:
            return {'status': 'failed', 'error': 'No available exchanges'}

        all_fills = []
        total_filled = 0.0
        total_cost = 0.0

        for i in range(slices):
            # 轮流选交易所（负载均衡）
            exchange_id = exchanges[i % len(exchanges)]

            result = await self.execute_single(
                symbol, side, slice_amount, exchange_id
            )

            if result.get('status') == 'filled':
                all_fills.append(result)
                total_filled += result['amount']
                total_cost += result['cost']

            if i < slices - 1:
                await asyncio.sleep(interval)

        avg_price = total_cost / total_filled if total_filled > 0 else 0

        return {
            'status': 'filled' if total_filled >= total_amount * 0.99 else 'partial',
            'symbol': symbol,
            'side': side,
            'requested': total_amount,
            'filled': total_filled,
            'avg_price': round(avg_price, 2),
            'total_cost': round(total_cost, 2),
            'slices': len(all_fills),
            'exchanges_used': list(set(f['exchange'] for f in all_fills)),
            'fills': all_fills,
        }


# ================================================================
# 跨交易所仓位同步器
# ================================================================

class PositionSynchronizer:
    """
    跨交易所仓位同步
    - 聚合所有交易所的持仓
    - 检测仓位偏差
    - 自动再平衡
    """

    def __init__(self, monitor: ExchangeHealthMonitor):
        self._monitor = monitor

    async def get_aggregated_positions(self) -> Dict[str, Dict]:
        """
        获取所有交易所的聚合持仓
        返回: {symbol: {total_size, avg_entry, by_exchange: {...}}}
        """
        tasks = {}
        for eid, exchange in self._monitor._exchanges.items():
            status = self._monitor._status.get(eid)
            if status and status.is_available:
                tasks[eid] = self._fetch_positions(exchange, eid)

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        # 聚合
        aggregated = defaultdict(lambda: {
            'total_size': 0.0,
            'total_cost': 0.0,
            'by_exchange': {},
            'side': '',
        })

        for eid, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                continue
            for pos in result:
                symbol = pos['symbol']
                size = pos['size']
                entry = pos.get('entry_price', 0)
                side = pos.get('side', 'long')

                agg = aggregated[symbol]
                agg['total_size'] += size
                agg['total_cost'] += size * entry
                agg['side'] = side
                agg['by_exchange'][eid] = {
                    'size': size,
                    'entry_price': entry,
                    'side': side,
                }

        # 计算均价
        for symbol, agg in aggregated.items():
            if agg['total_size'] > 0:
                agg['avg_entry'] = round(agg['total_cost'] / agg['total_size'], 2)
            else:
                agg['avg_entry'] = 0

        return dict(aggregated)

    async def _fetch_positions(self, exchange, exchange_id: str) -> List[Dict]:
        """获取单个交易所的持仓"""
        try:
            positions = await exchange.fetch_positions()
            result = []
            for pos in positions:
                size = float(pos.get('contracts', 0) or pos.get('amount', 0) or 0)
                if size == 0:
                    continue
                result.append({
                    'symbol': pos.get('symbol', ''),
                    'size': abs(size),
                    'entry_price': float(pos.get('entryPrice', 0) or 0),
                    'side': 'long' if size > 0 else 'short',
                    'unrealized_pnl': float(pos.get('unrealizedPnl', 0) or 0),
                })
            return result
        except Exception as e:
            logger.warning(f"Fetch positions failed on {exchange_id}: {e}")
            return []

    async def check_rebalance(self, target_symbol: str,
                               target_size: float,
                               tolerance_pct: float = 5.0) -> Optional[Dict]:
        """
        检查是否需要再平衡
        如果总持仓与目标偏差超过 tolerance，返回再平衡建议
        """
        positions = await self.get_aggregated_positions()
        current = positions.get(target_symbol, {})
        current_size = current.get('total_size', 0)

        if current_size == 0:
            return None

        deviation_pct = abs(current_size - target_size) / target_size * 100

        if deviation_pct > tolerance_pct:
            diff = target_size - current_size
            return {
                'symbol': target_symbol,
                'current_size': current_size,
                'target_size': target_size,
                'deviation_pct': round(deviation_pct, 2),
                'rebalance_amount': round(diff, 6),
                'rebalance_side': 'buy' if diff > 0 else 'sell',
                'by_exchange': current.get('by_exchange', {}),
            }
        return None


# ================================================================
# 统一多交易所管理器
# ================================================================

class MultiExchangeManager:
    """
    统一入口 — 管理多交易所连接 + 执行 + 监控
    """

    def __init__(self):
        self.monitor = ExchangeHealthMonitor()
        self.executor = MultiExchangeExecutor(self.monitor)
        self.price_aggregator = PriceAggregator(self.monitor)
        self.position_sync = PositionSynchronizer(self.monitor)

    async def add_exchange(self, exchange_id: str,
                            api_key: str = '', api_secret: str = '',
                            passphrase: str = ''):
        await self.monitor.add_exchange(exchange_id, api_key, api_secret, passphrase)

    async def execute(self, symbol: str, side: str, amount: float,
                       mode: str = 'auto', **kwargs) -> Dict:
        """
        统一执行接口

        mode:
        - 'auto': 自动选择最优交易所
        - 'parallel': 并行多所执行
        - 'arbitrage': 跨所套利
        - 'twap': 多所 TWAP
        """
        if mode == 'auto':
            return await self.executor.execute_single(symbol, side, amount, **kwargs)
        elif mode == 'parallel':
            return await self.executor.execute_parallel(symbol, side, amount, **kwargs)
        elif mode == 'arbitrage':
            return await self.executor.execute_arbitrage(symbol, amount, **kwargs)
        elif mode == 'twap':
            return await self.executor.execute_twap_multi(symbol, side, amount, **kwargs)
        else:
            return {'status': 'failed', 'error': f'Unknown mode: {mode}'}

    def get_status(self) -> Dict:
        return {
            'exchanges': self.monitor.get_status(),
            'execution_stats': {
                'total_orders': len(self.executor._order_history),
            },
        }

    async def stop(self):
        await self.monitor.stop()


# ================================================================
# 全局单例
# ================================================================

multi_exchange = MultiExchangeManager()
