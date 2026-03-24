"""
MyTradingPlatform — 高性能数据层
WebSocket 实时数据流 + OrderBook 管理 + 数据聚合

Phase 1: 数据处理层
- WebSocket 实时 K 线 / 逐笔成交 / OrderBook
- 本地 OrderBook 维护 (L2 深度)
- 数据缓存与聚合管道
- 多交易所数据源管理
"""

import time
import json
import logging
import threading
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import hashlib

logger = logging.getLogger(__name__)

# ================================================================
# 数据结构
# ================================================================

class DataType(Enum):
    OHLCV = "ohlcv"
    TRADES = "trades"
    ORDER_BOOK = "order_book"
    TICKER = "ticker"
    FUNDING_RATE = "funding_rate"
    LIQUIDATIONS = "liquidations"


@dataclass
class OrderBookLevel:
    price: float
    amount: float
    count: int = 1  # 订单数（部分交易所提供）


@dataclass
class OrderBook:
    symbol: str = ""
    exchange: str = ""
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0
    sequence: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def spread(self) -> float:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return 0.0

    @property
    def spread_pct(self) -> float:
        if self.bids and self.asks:
            mid = (self.asks[0].price + self.bids[0].price) / 2
            return self.spread / mid * 100 if mid > 0 else 0
        return 0.0

    @property
    def mid_price(self) -> float:
        if self.bids and self.asks:
            return (self.asks[0].price + self.bids[0].price) / 2
        return 0.0

    def depth(self, side: str, levels: int = 10) -> List[Tuple[float, float]]:
        """获取指定方向的深度"""
        book = self.bids if side == 'bids' else self.asks
        return [(lv.price, lv.amount) for lv in book[:levels]]

    def volume_at_price(self, side: str, target_price: float, tolerance_pct: float = 0.1) -> float:
        """计算目标价位附近的挂单量"""
        book = self.bids if side == 'bids' else self.asks
        total = 0.0
        for lv in book:
            if abs(lv.price - target_price) / target_price * 100 <= tolerance_pct:
                total += lv.amount
            else:
                break
        return total

    def imbalance_ratio(self, levels: int = 10) -> float:
        """买卖盘失衡比 (bid_vol / ask_vol)，>1 说明买盘更厚"""
        bid_vol = sum(lv.amount for lv in self.bids[:levels])
        ask_vol = sum(lv.amount for lv in self.asks[:levels])
        if ask_vol == 0:
            return float('inf')
        return bid_vol / ask_vol

    def vwap(self, side: str, target_amount: float) -> float:
        """计算吃掉 target_amount 的 VWAP"""
        book = self.bids if side == 'bids' else self.asks
        remaining = target_amount
        cost = 0.0
        filled = 0.0
        for lv in book:
            if remaining <= 0:
                break
            fill = min(remaining, lv.amount)
            cost += fill * lv.price
            filled += fill
            remaining -= fill
        return cost / filled if filled > 0 else 0.0


@dataclass
class Ticker:
    symbol: str = ""
    exchange: str = ""
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    quote_volume: float = 0.0
    change_pct: float = 0.0
    timestamp: float = 0.0


@dataclass
class Trade:
    id: str = ""
    symbol: str = ""
    exchange: str = ""
    side: str = ""  # buy/sell
    price: float = 0.0
    amount: float = 0.0
    cost: float = 0.0
    timestamp: float = 0.0
    is_liquidation: bool = False


# ================================================================
# 回调管理器
# ================================================================

class CallbackRegistry:
    """发布/订阅回调管理"""

    def __init__(self):
        self._callbacks: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable):
        with self._lock:
            self._callbacks[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        with self._lock:
            if callback in self._callbacks[event_type]:
                self._callbacks[event_type].remove(callback)

    def publish(self, event_type: str, data: Any):
        with self._lock:
            cbs = list(self._callbacks[event_type])
        for cb in cbs:
            try:
                cb(data)
            except Exception as e:
                logger.error(f"Callback error ({event_type}): {e}")


# ================================================================
# K 线数据管理器 — 带实时更新
# ================================================================

class KlineManager:
    """
    管理实时 K 线数据
    支持通过 REST 初始加载 + WebSocket 实时更新
    """

    def __init__(self, max_bars: int = 2000):
        self.max_bars = max_bars
        self._data: Dict[str, pd.DataFrame] = {}  # key: f"{exchange}:{symbol}:{tf}"
        self._lock = threading.Lock()
        self.callbacks = CallbackRegistry()

    def _key(self, exchange: str, symbol: str, timeframe: str) -> str:
        return f"{exchange}:{symbol}:{timeframe}"

    def initialize(self, exchange: str, symbol: str, timeframe: str, df: pd.DataFrame):
        """用历史数据初始化"""
        key = self._key(exchange, symbol, timeframe)
        with self._lock:
            self._data[key] = df.tail(self.max_bars).copy()

    def update_bar(self, exchange: str, symbol: str, timeframe: str,
                   timestamp: float, open_: float, high: float,
                   low: float, close: float, volume: float):
        """更新或追加一根 K 线（WebSocket 回调用）"""
        key = self._key(exchange, symbol, timeframe)
        ts = pd.Timestamp(timestamp, unit='ms')

        with self._lock:
            if key not in self._data:
                self._data[key] = pd.DataFrame(
                    columns=['open', 'high', 'low', 'close', 'volume']
                )

            df = self._data[key]
            if ts in df.index:
                # 更新当前未收盘的 K 线
                df.loc[ts, 'high'] = max(df.loc[ts, 'high'], high)
                df.loc[ts, 'low'] = min(df.loc[ts, 'low'], low)
                df.loc[ts, 'close'] = close
                df.loc[ts, 'volume'] = volume
            else:
                # 新 K 线
                new_row = pd.DataFrame(
                    {'open': [open_], 'high': [high], 'low': [low],
                     'close': [close], 'volume': [volume]},
                    index=[ts]
                )
                self._data[key] = pd.concat([df, new_row]).tail(self.max_bars)

            df_out = self._data[key]

        # 通知订阅者
        self.callbacks.publish(f'kline:{key}', {
            'symbol': symbol, 'timeframe': timeframe,
            'timestamp': ts, 'close': close, 'volume': volume
        })

    def get(self, exchange: str, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """获取当前 K 线数据"""
        key = self._key(exchange, symbol, timeframe)
        with self._lock:
            df = self._data.get(key)
            return df.copy() if df is not None else None

    def latest_price(self, exchange: str, symbol: str, timeframe: str = '1h') -> Optional[float]:
        key = self._key(exchange, symbol, timeframe)
        with self._lock:
            df = self._data.get(key)
            if df is not None and len(df) > 0:
                return float(df.iloc[-1]['close'])
        return None


# ================================================================
# OrderBook 管理器 — L2 深度维护
# ================================================================

class OrderBookManager:
    """
    本地 OrderBook 维护
    支持快照 + 增量更新 (diff) 模式
    """

    def __init__(self, depth_levels: int = 20):
        self.depth_levels = depth_levels
        self._books: Dict[str, OrderBook] = {}
        self._snapshots: Dict[str, List[float]] = {}  # 用于检测异常
        self._lock = threading.Lock()
        self.callbacks = CallbackRegistry()

    def _key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def update_snapshot(self, exchange: str, symbol: str,
                        bids: List[List[float]], asks: List[List[float]],
                        timestamp: float = None, sequence: int = 0):
        """更新 OrderBook 快照"""
        key = self._key(exchange, symbol)
        ts = timestamp or time.time()

        bid_levels = [OrderBookLevel(price=float(b[0]), amount=float(b[1]),
                                     count=int(b[2]) if len(b) > 2 else 1)
                      for b in bids[:self.depth_levels]]
        ask_levels = [OrderBookLevel(price=float(a[0]), amount=float(a[1]),
                                     count=int(a[2]) if len(a) > 2 else 1)
                      for a in asks[:self.depth_levels]]

        book = OrderBook(
            symbol=symbol, exchange=exchange,
            bids=bid_levels, asks=ask_levels,
            timestamp=ts, sequence=sequence
        )

        with self._lock:
            self._books[key] = book

        self.callbacks.publish(f'orderbook:{key}', book)

    def get(self, exchange: str, symbol: str) -> Optional[OrderBook]:
        with self._lock:
            return self._books.get(self._key(exchange, symbol))

    def get_spread(self, exchange: str, symbol: str) -> Optional[float]:
        book = self.get(exchange, symbol)
        return book.spread_pct if book else None

    def get_imbalance(self, exchange: str, symbol: str, levels: int = 10) -> Optional[float]:
        book = self.get(exchange, symbol)
        return book.imbalance_ratio(levels) if book else None

    def get_all_symbols(self) -> List[str]:
        with self._lock:
            return list(self._books.keys())


# ================================================================
# 逐笔成交管理器
# ================================================================

class TradeStreamManager:
    """管理实时逐笔成交数据，用于异常检测和大单追踪"""

    def __init__(self, window_size: int = 1000):
        self.window_size = window_size
        self._trades: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size))
        self._stats: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self.callbacks = CallbackRegistry()

    def _key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def add_trade(self, trade: Trade):
        key = self._key(trade.exchange, trade.symbol)
        with self._lock:
            self._trades[key].append(trade)
            self._update_stats(key)

        self.callbacks.publish(f'trade:{key}', trade)

        # 大单检测（超过平均交易量 10 倍）
        stats = self.get_stats(trade.exchange, trade.symbol)
        if stats and trade.amount > stats['avg_amount'] * 10:
            self.callbacks.publish(f'large_trade:{key}', {
                'trade': trade,
                'multiple': trade.amount / stats['avg_amount'] if stats['avg_amount'] > 0 else 0,
            })

    def _update_stats(self, key: str):
        trades = list(self._trades[key])
        if not trades:
            return
        amounts = [t.amount for t in trades]
        prices = [t.price for t in trades]
        buy_vol = sum(t.amount for t in trades if t.side == 'buy')
        sell_vol = sum(t.amount for t in trades if t.side == 'sell')

        self._stats[key] = {
            'count': len(trades),
            'avg_amount': np.mean(amounts),
            'median_amount': np.median(amounts),
            'avg_price': np.mean(prices),
            'buy_volume': buy_vol,
            'sell_volume': sell_vol,
            'buy_sell_ratio': buy_vol / sell_vol if sell_vol > 0 else float('inf'),
            'last_price': trades[-1].price,
            'last_ts': trades[-1].timestamp,
        }

    def get_stats(self, exchange: str, symbol: str) -> Optional[Dict]:
        key = self._key(exchange, symbol)
        with self._lock:
            return self._stats.get(key)

    def get_recent(self, exchange: str, symbol: str, n: int = 100) -> List[Trade]:
        key = self._key(exchange, symbol)
        with self._lock:
            trades = list(self._trades.get(key, []))
        return trades[-n:]

    def detect_whale_activity(self, exchange: str, symbol: str,
                               threshold_multiplier: float = 10.0,
                               window_seconds: int = 300) -> List[Dict]:
        """检测大额交易活动（鲸鱼预警）"""
        key = self._key(exchange, symbol)
        with self._lock:
            trades = list(self._trades.get(key, []))

        if not trades:
            return []

        now = time.time()
        recent = [t for t in trades if now - t.timestamp < window_seconds]
        if not recent:
            return []

        amounts = [t.amount for t in trades]
        avg = np.mean(amounts) if amounts else 0
        if avg == 0:
            return []

        whales = []
        for t in recent:
            if t.amount > avg * threshold_multiplier:
                whales.append({
                    'time': datetime.fromtimestamp(t.timestamp).strftime('%H:%M:%S'),
                    'side': t.side,
                    'price': t.price,
                    'amount': t.amount,
                    'cost': t.cost,
                    'multiple': round(t.amount / avg, 1),
                    'is_liquidation': t.is_liquidation,
                })

        return whales


# ================================================================
# WebSocket 数据源管理器
# ================================================================

class WebSocketFeed:
    """
    WebSocket 实时数据源
    使用 ccxt WebSocket API（ccxt.pro 或 ccxt async support）

    设计为后台线程运行，通过回调分发数据
    """

    def __init__(self, exchange_id: str = 'binance',
                 api_key: str = '', api_secret: str = ''):
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.api_secret = api_secret
        self._exchange = None
        self._running = False
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

        # 管理器
        self.klines = KlineManager()
        self.orderbook = OrderBookManager()
        self.trades = TradeStreamManager()
        self.callbacks = CallbackRegistry()

        # 连接状态
        self._connection_status: Dict[str, str] = {}  # key -> connected/error/disconnected
        self._last_heartbeat: Dict[str, float] = {}

    def _get_exchange(self):
        """延迟初始化 ccxt 交易所实例"""
        if self._exchange is None:
            import ccxt
            cls = getattr(ccxt, self.exchange_id)
            params = {'enableRateLimit': True, 'options': {'defaultType': 'swap'}}
            if self.api_key:
                params['apiKey'] = self.api_key
                params['secret'] = self.api_secret
            self._exchange = cls(params)
        return self._exchange

    def start_kline_stream(self, symbol: str, timeframe: str = '1h'):
        """启动 K 线 WebSocket 流"""
        key = f"kline:{symbol}:{timeframe}"
        if key in self._threads and self._threads[key].is_alive():
            return

        stop_event = threading.Event()
        self._stop_events[key] = stop_event

        t = threading.Thread(
            target=self._kline_loop,
            args=(symbol, timeframe, stop_event),
            daemon=True, name=f"ws-kline-{symbol}"
        )
        self._threads[key] = t
        t.start()
        logger.info(f"Kline stream started: {symbol} {timeframe}")

    def start_orderbook_stream(self, symbol: str, limit: int = 20):
        """启动 OrderBook WebSocket 流"""
        key = f"ob:{symbol}"
        if key in self._threads and self._threads[key].is_alive():
            return

        stop_event = threading.Event()
        self._stop_events[key] = stop_event

        t = threading.Thread(
            target=self._orderbook_loop,
            args=(symbol, limit, stop_event),
            daemon=True, name=f"ws-ob-{symbol}"
        )
        self._threads[key] = t
        t.start()
        logger.info(f"OrderBook stream started: {symbol}")

    def start_trades_stream(self, symbol: str):
        """启动逐笔成交 WebSocket 流"""
        key = f"trades:{symbol}"
        if key in self._threads and self._threads[key].is_alive():
            return

        stop_event = threading.Event()
        self._stop_events[key] = stop_event

        t = threading.Thread(
            target=self._trades_loop,
            args=(symbol, stop_event),
            daemon=True, name=f"ws-trades-{symbol}"
        )
        self._threads[key] = t
        t.start()
        logger.info(f"Trades stream started: {symbol}")

    def stop_all(self):
        """停止所有流"""
        for key, event in self._stop_events.items():
            event.set()
        for key, t in self._threads.items():
            t.join(timeout=5)
        self._threads.clear()
        self._stop_events.clear()
        logger.info("All WebSocket streams stopped")

    def stop_stream(self, key_pattern: str):
        """停止匹配的流"""
        keys_to_stop = [k for k in self._stop_events if key_pattern in k]
        for key in keys_to_stop:
            self._stop_events[key].set()
            if key in self._threads:
                self._threads[key].join(timeout=5)
                del self._threads[key]
            del self._stop_events[key]

    def get_status(self) -> Dict:
        """获取所有流的状态"""
        status = {}
        for key, thread in self._threads.items():
            status[key] = {
                'alive': thread.is_alive(),
                'connection': self._connection_status.get(key, 'unknown'),
                'last_heartbeat': self._last_heartbeat.get(key, 0),
                'age_seconds': time.time() - self._last_heartbeat.get(key, time.time()),
            }
        return status

    # ---- 内部循环（带自动重连） ----

    def _kline_loop(self, symbol: str, timeframe: str, stop_event: threading.Event):
        key = f"kline:{symbol}:{timeframe}"
        while not stop_event.is_set():
            try:
                self._connection_status[key] = 'connected'
                exchange = self._get_exchange()

                # 初始加载历史数据
                try:
                    data = exchange.fetch_ohlcv(symbol, timeframe, limit=500)
                    if data:
                        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        df.set_index('timestamp', inplace=True)
                        df = df.astype(float)
                        self.klines.initialize(self.exchange_id, symbol, timeframe, df)
                        logger.info(f"Kline initialized: {symbol} {timeframe} ({len(df)} bars)")
                except Exception as e:
                    logger.warning(f"Initial kline load failed: {e}")

                # WebSocket 循环
                while not stop_event.is_set():
                    try:
                        # ccxt WebSocket: watch_ohlcv
                        if hasattr(exchange, 'watch_ohlcv'):
                            candles = exchange.watch_ohlcv(symbol, timeframe)
                            if candles:
                                for c in candles[-1:]:
                                    self.klines.update_bar(
                                        self.exchange_id, symbol, timeframe,
                                        timestamp=c[0], open_=c[1], high=c[2],
                                        low=c[3], close=c[4], volume=c[5]
                                    )
                                    self._last_heartbeat[key] = time.time()
                        else:
                            # Fallback: 轮询模式
                            data = exchange.fetch_ohlcv(symbol, timeframe, limit=2)
                            if data:
                                c = data[-1]
                                self.klines.update_bar(
                                    self.exchange_id, symbol, timeframe,
                                    timestamp=c[0], open_=c[1], high=c[2],
                                    low=c[3], close=c[4], volume=c[5]
                                )
                            self._last_heartbeat[key] = time.time()
                            # 根据 timeframe 决定轮询间隔
                            poll_interval = {'1m': 5, '5m': 15, '15m': 30,
                                           '30m': 60, '1h': 120, '4h': 300, '1d': 600}
                            stop_event.wait(poll_interval.get(timeframe, 120))

                    except Exception as e:
                        logger.error(f"Kline WS error ({symbol} {timeframe}): {e}")
                        self._connection_status[key] = 'error'
                        stop_event.wait(5)  # 重连等待

            except Exception as e:
                logger.error(f"Kline loop fatal error: {e}")
                self._connection_status[key] = 'error'
                stop_event.wait(10)

        self._connection_status[key] = 'disconnected'
        logger.info(f"Kline stream stopped: {symbol} {timeframe}")

    def _orderbook_loop(self, symbol: str, limit: int, stop_event: threading.Event):
        key = f"ob:{symbol}"
        while not stop_event.is_set():
            try:
                self._connection_status[key] = 'connected'
                exchange = self._get_exchange()

                while not stop_event.is_set():
                    try:
                        if hasattr(exchange, 'watch_order_book'):
                            book = exchange.watch_order_book(symbol, limit)
                            self.orderbook.update_snapshot(
                                self.exchange_id, symbol,
                                bids=book.get('bids', []),
                                asks=book.get('asks', []),
                                timestamp=book.get('timestamp', time.time() * 1000) / 1000,
                                sequence=book.get('nonce', 0),
                            )
                        else:
                            # Fallback: REST 轮询
                            book = exchange.fetch_order_book(symbol, limit)
                            self.orderbook.update_snapshot(
                                self.exchange_id, symbol,
                                bids=book.get('bids', []),
                                asks=book.get('asks', []),
                                timestamp=book.get('timestamp', time.time() * 1000) / 1000,
                            )
                            stop_event.wait(2)  # OrderBook 更新频率更高

                        self._last_heartbeat[key] = time.time()

                    except Exception as e:
                        logger.error(f"OrderBook WS error ({symbol}): {e}")
                        self._connection_status[key] = 'error'
                        stop_event.wait(3)

            except Exception as e:
                logger.error(f"OrderBook loop fatal error: {e}")
                self._connection_status[key] = 'error'
                stop_event.wait(10)

        self._connection_status[key] = 'disconnected'

    def _trades_loop(self, symbol: str, stop_event: threading.Event):
        key = f"trades:{symbol}"
        while not stop_event.is_set():
            try:
                self._connection_status[key] = 'connected'
                exchange = self._get_exchange()

                while not stop_event.is_set():
                    try:
                        if hasattr(exchange, 'watch_trades'):
                            raw_trades = exchange.watch_trades(symbol)
                            for t in raw_trades[-10:]:
                                trade = Trade(
                                    id=str(t.get('id', '')),
                                    symbol=symbol,
                                    exchange=self.exchange_id,
                                    side=t.get('side', ''),
                                    price=float(t.get('price', 0)),
                                    amount=float(t.get('amount', 0)),
                                    cost=float(t.get('cost', 0)),
                                    timestamp=t.get('timestamp', time.time() * 1000) / 1000,
                                )
                                self.trades.add_trade(trade)
                        else:
                            # Fallback: REST 轮询
                            raw = exchange.fetch_trades(symbol, limit=10)
                            for t in raw[-5:]:
                                trade = Trade(
                                    id=str(t.get('id', '')),
                                    symbol=symbol,
                                    exchange=self.exchange_id,
                                    side=t.get('side', ''),
                                    price=float(t.get('price', 0)),
                                    amount=float(t.get('amount', 0)),
                                    cost=float(t.get('cost', 0)),
                                    timestamp=t.get('timestamp', time.time() * 1000) / 1000,
                                )
                                self.trades.add_trade(trade)
                            stop_event.wait(3)

                        self._last_heartbeat[key] = time.time()

                    except Exception as e:
                        logger.error(f"Trades WS error ({symbol}): {e}")
                        self._connection_status[key] = 'error'
                        stop_event.wait(3)

            except Exception as e:
                logger.error(f"Trades loop fatal error: {e}")
                self._connection_status[key] = 'error'
                stop_event.wait(10)

        self._connection_status[key] = 'disconnected'


# ================================================================
# 数据聚合管理器 — 统一入口
# ================================================================

class DataManager:
    """
    统一数据管理入口
    管理多交易所 WebSocket 数据源，提供数据查询接口
    """

    def __init__(self):
        self._feeds: Dict[str, WebSocketFeed] = {}
        self._lock = threading.Lock()
        self.callbacks = CallbackRegistry()

    def add_exchange(self, exchange_id: str,
                     api_key: str = '', api_secret: str = '') -> WebSocketFeed:
        """添加交易所数据源"""
        with self._lock:
            if exchange_id not in self._feeds:
                feed = WebSocketFeed(exchange_id, api_key, api_secret)
                self._feeds[exchange_id] = feed
                logger.info(f"Data source added: {exchange_id}")
            return self._feeds[exchange_id]

    def get_feed(self, exchange_id: str) -> Optional[WebSocketFeed]:
        return self._feeds.get(exchange_id)

    def subscribe_symbol(self, exchange_id: str, symbol: str,
                          timeframe: str = '1h',
                          streams: List[str] = None):
        """
        订阅一个交易对的数据流
        streams: ['kline', 'orderbook', 'trades'] 默认全部
        """
        if streams is None:
            streams = ['kline', 'orderbook', 'trades']

        feed = self.get_feed(exchange_id)
        if not feed:
            feed = self.add_exchange(exchange_id)

        if 'kline' in streams:
            feed.start_kline_stream(symbol, timeframe)
        if 'orderbook' in streams:
            feed.start_orderbook_stream(symbol)
        if 'trades' in streams:
            feed.start_trades_stream(symbol)

    def get_kline(self, exchange_id: str, symbol: str,
                  timeframe: str = '1h') -> Optional[pd.DataFrame]:
        feed = self.get_feed(exchange_id)
        if feed:
            return feed.klines.get(exchange_id, symbol, timeframe)
        return None

    def get_orderbook(self, exchange_id: str, symbol: str) -> Optional[OrderBook]:
        feed = self.get_feed(exchange_id)
        if feed:
            return feed.orderbook.get(exchange_id, symbol)
        return None

    def get_trade_stats(self, exchange_id: str, symbol: str) -> Optional[Dict]:
        feed = self.get_feed(exchange_id)
        if feed:
            return feed.trades.get_stats(exchange_id, symbol)
        return None

    def get_latest_price(self, exchange_id: str, symbol: str,
                         timeframe: str = '1h') -> Optional[float]:
        feed = self.get_feed(exchange_id)
        if feed:
            return feed.klines.latest_price(exchange_id, symbol, timeframe)
        return None

    def get_market_snapshot(self, exchange_id: str, symbol: str) -> Dict:
        """获取一个交易对的完整市场快照"""
        result = {
            'symbol': symbol,
            'exchange': exchange_id,
            'timestamp': datetime.now().isoformat(),
        }

        feed = self.get_feed(exchange_id)
        if not feed:
            result['error'] = 'Exchange not connected'
            return result

        # K 线数据
        df = feed.klines.get(exchange_id, symbol, '1h')
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            result['price'] = float(last['close'])
            result['volume_24h'] = float(df.tail(24)['volume'].sum()) if len(df) >= 24 else float(df['volume'].sum())

        # OrderBook
        ob = feed.orderbook.get(exchange_id, symbol)
        if ob:
            result['orderbook'] = {
                'best_bid': ob.best_bid,
                'best_ask': ob.best_ask,
                'spread_pct': round(ob.spread_pct, 4),
                'mid_price': ob.mid_price,
                'imbalance': round(ob.imbalance_ratio(10), 3),
            }

        # 成交统计
        stats = feed.trades.get_stats(exchange_id, symbol)
        if stats:
            result['trades'] = {
                'buy_sell_ratio': round(stats.get('buy_sell_ratio', 0), 3),
                'avg_amount': round(stats.get('avg_amount', 0), 4),
                'count': stats.get('count', 0),
            }

        return result

    def stop_all(self):
        """停止所有数据流"""
        for feed in self._feeds.values():
            feed.stop_all()
        logger.info("All data feeds stopped")

    def get_all_status(self) -> Dict:
        """获取所有数据源状态"""
        status = {}
        for eid, feed in self._feeds.items():
            status[eid] = feed.get_status()
        return status


# ================================================================
# 全局单例
# ================================================================

data_manager = DataManager()
