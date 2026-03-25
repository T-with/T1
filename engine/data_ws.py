"""
MyTradingPlatform — WebSocket 实时数据引擎
真正的 WebSocket 推送 + 自动重连 + 序列号校验 + asyncio

Phase 1 核心升级:
- asyncio 原生 WebSocket（非 threading 轮询）
- 指数退避自动重连
- OrderBook 快照 + 增量 diff 序列号校验
- 统一事件总线，策略引擎零延迟消费
- 多交易所并行连接
"""

import asyncio
import aiohttp
import json
import time
import logging
import hashlib
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Tuple, Any, Set
from dataclasses import dataclass, field
from enum import Enum
import websockets
import ssl

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
CACHE_DIR = DATA_DIR / 'cache'
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ================================================================
# 数据结构（与原 data.py 兼容）
# ================================================================

class DataType(Enum):
    OHLCV = "ohlcv"
    TRADES = "trades"
    ORDER_BOOK = "order_book"
    TICKER = "ticker"
    FUNDING_RATE = "funding_rate"
    LIQUIDATIONS = "liquidations"
    BOOK_TICKER = "book_ticker"


@dataclass
class OrderBookLevel:
    price: float
    amount: float
    count: int = 1


@dataclass
class OrderBook:
    symbol: str = ""
    exchange: str = ""
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0
    last_update_id: int = 0

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

    def imbalance_ratio(self, levels: int = 10) -> float:
        bid_vol = sum(lv.amount for lv in self.bids[:levels])
        ask_vol = sum(lv.amount for lv in self.asks[:levels])
        if ask_vol == 0:
            return float('inf')
        return bid_vol / ask_vol

    def vwap(self, side: str, target_amount: float) -> float:
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
    side: str = ""
    price: float = 0.0
    amount: float = 0.0
    cost: float = 0.0
    timestamp: float = 0.0
    is_liquidation: bool = False


@dataclass
class Liquidation:
    symbol: str = ""
    exchange: str = ""
    side: str = ""
    price: float = 0.0
    amount: float = 0.0
    timestamp: float = 0.0


# ================================================================
# 事件总线 — 发布/订阅
# ================================================================

class EventBus:
    """高性能异步事件总线"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._async_subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, event_type: str, callback: Callable):
        """注册同步回调"""
        self._subscribers[event_type].append(callback)

    def subscribe_async(self, event_type: str, callback: Callable):
        """注册异步回调"""
        self._async_subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        if callback in self._subscribers[event_type]:
            self._subscribers[event_type].remove(callback)
        if callback in self._async_subscribers[event_type]:
            self._async_subscribers[event_type].remove(callback)

    async def publish(self, event_type: str, data: Any):
        """发布事件到所有订阅者"""
        # 同步回调在线程池中执行
        for cb in list(self._subscribers[event_type]):
            try:
                cb(data)
            except Exception as e:
                logger.error(f"Sync callback error ({event_type}): {e}")

        # 异步回调直接 await
        for cb in list(self._async_subscribers[event_type]):
            try:
                await cb(data)
            except Exception as e:
                logger.error(f"Async callback error ({event_type}): {e}")

    def publish_sync(self, event_type: str, data: Any):
        """同步发布（用于非 async 上下文）"""
        for cb in list(self._subscribers[event_type]):
            try:
                cb(data)
            except Exception as e:
                logger.error(f"Sync callback error ({event_type}): {e}")


# ================================================================
# WebSocket 连接管理 — 指数退避重连
# ================================================================

class WSConnection:
    """
    单个 WebSocket 连接的管理
    - 指数退避自动重连
    - 心跳检测
    - 消息队列缓冲
    """

    def __init__(self, url: str, name: str,
                 on_message: Callable,
                 on_connect: Optional[Callable] = None,
                 on_disconnect: Optional[Callable] = None,
                 ping_interval: float = 20.0,
                 max_reconnect_delay: float = 60.0):
        self.url = url
        self.name = name
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.ping_interval = ping_interval
        self.max_reconnect_delay = max_reconnect_delay

        self._ws = None
        self._running = False
        self._reconnect_count = 0
        self._last_message_time = 0.0
        self._connected = False
        self._message_count = 0

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> Dict:
        return {
            'name': self.name,
            'connected': self._connected,
            'reconnect_count': self._reconnect_count,
            'message_count': self._message_count,
            'last_message_ago': round(time.time() - self._last_message_time, 1) if self._last_message_time else None,
        }

    async def start(self):
        """启动连接（带自动重连）"""
        self._running = True
        while self._running:
            try:
                delay = min(2 ** self._reconnect_count, self.max_reconnect_delay)
                if self._reconnect_count > 0:
                    logger.info(f"[{self.name}] Reconnecting in {delay}s (attempt {self._reconnect_count})")
                    await asyncio.sleep(delay)

                async with websockets.connect(
                    self.url,
                    ping_interval=self.ping_interval,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20,  # 1MB max message
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_count = 0
                    logger.info(f"[{self.name}] Connected to {self.url[:60]}...")

                    if self.on_connect:
                        await self.on_connect(ws)

                    async for message in ws:
                        self._last_message_time = time.time()
                        self._message_count += 1
                        try:
                            await self.on_message(message)
                        except Exception as e:
                            logger.error(f"[{self.name}] Message handler error: {e}")

            except websockets.ConnectionClosed as e:
                logger.warning(f"[{self.name}] Connection closed: {e}")
            except ConnectionRefusedError:
                logger.warning(f"[{self.name}] Connection refused")
            except Exception as e:
                logger.error(f"[{self.name}] Unexpected error: {e}")
            finally:
                self._connected = False
                self._reconnect_count += 1
                if self.on_disconnect:
                    try:
                        await self.on_disconnect()
                    except Exception:
                        pass

        logger.info(f"[{self.name}] Stopped")

    async def stop(self):
        """停止连接"""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def send(self, data: Any):
        """发送消息"""
        if self._ws and self._connected:
            if isinstance(data, dict):
                data = json.dumps(data)
            await self._ws.send(data)


# ================================================================
# Binance WebSocket 适配器
# ================================================================

class BinanceWSAdapter:
    """
    Binance WebSocket 专用适配器
    支持: K线 / 逐笔成交 / OrderBook(深度) / BookTicker / 清算
    文档: https://binance-docs.github.io/apidocs/futures/en/#websocket-market-streams
    """

    WS_BASE = "wss://fstream.binance.com/ws"
    WS_BASE_SPOT = "wss://stream.binance.com:9443/ws"

    def __init__(self, exchange_type: str = 'future'):
        self.exchange_type = exchange_type
        self.base_url = self.WS_BASE if exchange_type == 'future' else self.WS_BASE_SPOT
        self._connections: Dict[str, WSConnection] = {}
        self._running = False

    def _ws_symbol(self, symbol: str) -> str:
        """BTC/USDT -> btcusdt"""
        return symbol.replace('/', '').lower()

    # ---- K线流 ----

    async def subscribe_kline(self, symbol: str, timeframe: str,
                               callback: Callable, bus: EventBus):
        """订阅实时 K 线"""
        ws_sym = self._ws_symbol(symbol)
        stream = f"{ws_sym}@kline_{timeframe}"
        url = f"{self.base_url}/{stream}"
        key = f"kline:{symbol}:{timeframe}"

        async def on_message(raw):
            data = json.loads(raw)
            k = data.get('k', {})
            await bus.publish(f'kline:{symbol}:{timeframe}', {
                'symbol': symbol,
                'timeframe': timeframe,
                'timestamp': k.get('t', 0),
                'open': float(k.get('o', 0)),
                'high': float(k.get('h', 0)),
                'low': float(k.get('l', 0)),
                'close': float(k.get('c', 0)),
                'volume': float(k.get('v', 0)),
                'is_closed': k.get('x', False),
            })

        conn = WSConnection(url, key, on_message)
        self._connections[key] = conn
        return conn

    # ---- 逐笔成交流 ----

    async def subscribe_trades(self, symbol: str, bus: EventBus):
        """订阅逐笔成交"""
        ws_sym = self._ws_symbol(symbol)
        stream = f"{ws_sym}@aggTrade"
        url = f"{self.base_url}/{stream}"
        key = f"trades:{symbol}"

        async def on_message(raw):
            data = json.loads(raw)
            trade = Trade(
                id=str(data.get('a', '')),
                symbol=symbol,
                exchange='binance',
                side='sell' if data.get('m', False) else 'buy',  # m=true -> buyer is maker -> sell
                price=float(data.get('p', 0)),
                amount=float(data.get('q', 0)),
                cost=float(data.get('p', 0)) * float(data.get('q', 0)),
                timestamp=data.get('T', 0) / 1000,
            )
            await bus.publish(f'trade:{symbol}', trade)

        conn = WSConnection(url, key, on_message)
        self._connections[key] = conn
        return conn

    # ---- 深度流（diff depth） ----

    async def subscribe_depth(self, symbol: str, bus: EventBus,
                               levels: int = 20):
        """
        订阅增量深度流
        Binance diff depth stream: 100ms 推送
        需要先获取一次快照，然后增量更新
        """
        ws_sym = self._ws_symbol(symbol)
        stream = f"{ws_sym}@depth@100ms"
        url = f"{self.base_url}/{stream}"
        key = f"depth:{symbol}"

        # OrderBook 状态
        book_state = {
            'bids': {},  # price -> amount
            'asks': {},
            'last_update_id': 0,
            'initialized': False,
            'buffer': [],  # 缓存初始化前到达的消息
        }

        async def load_snapshot():
            """从 REST API 获取 OrderBook 快照"""
            rest_url = f"https://fapi.binance.com/fapi/v1/depth?symbol={ws_sym.upper()}&limit=1000"
            if self.exchange_type == 'spot':
                rest_url = f"https://api.binance.com/api/v3/depth?symbol={ws_sym.upper()}&limit=1000"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(rest_url) as resp:
                        data = await resp.json()
                        book_state['bids'] = {float(b[0]): float(b[1]) for b in data.get('bids', [])}
                        book_state['asks'] = {float(a[0]): float(a[1]) for a in data.get('asks', [])}
                        book_state['last_update_id'] = data.get('lastUpdateId', 0)
                        book_state['initialized'] = True
                        logger.info(f"[depth:{symbol}] Snapshot loaded, lastUpdateId={book_state['last_update_id']}")
            except Exception as e:
                logger.error(f"[depth:{symbol}] Snapshot failed: {e}")

        async def on_connect(ws):
            """连接后加载快照"""
            await load_snapshot()
            # 处理缓冲区中有效消息
            for msg in book_state['buffer']:
                await process_depth_update(msg)
            book_state['buffer'].clear()

        async def process_depth_update(data):
            """处理深度增量更新"""
            pu = data.get('pu', 0)  # previous update id
            u = data.get('u', 0)    # final update id

            if not book_state['initialized']:
                book_state['buffer'].append(data)
                return

            # 序列号校验: u 必须 > last_update_id
            if u <= book_state['last_update_id']:
                return  # 旧数据，丢弃

            # 更新 bids
            for bid in data.get('b', []):
                price = float(bid[0])
                amount = float(bid[1])
                if amount == 0:
                    book_state['bids'].pop(price, None)
                else:
                    book_state['bids'][price] = amount

            # 更新 asks
            for ask in data.get('a', []):
                price = float(ask[0])
                amount = float(ask[1])
                if amount == 0:
                    book_state['asks'].pop(price, None)
                else:
                    book_state['asks'][price] = amount

            book_state['last_update_id'] = u

            # 每 10 次更新发布一次完整 OrderBook（节省 CPU）
            if book_state['last_update_id'] % 10 == 0:
                sorted_bids = sorted(book_state['bids'].items(), reverse=True)[:levels]
                sorted_asks = sorted(book_state['asks'].items())[:levels]

                book = OrderBook(
                    symbol=symbol,
                    exchange='binance',
                    bids=[OrderBookLevel(price=p, amount=a) for p, a in sorted_bids],
                    asks=[OrderBookLevel(price=p, amount=a) for p, a in sorted_asks],
                    timestamp=time.time(),
                    last_update_id=u,
                )
                await bus.publish(f'orderbook:{symbol}', book)

        async def on_message(raw):
            data = json.loads(raw)
            await process_depth_update(data)

        conn = WSConnection(url, key, on_message, on_connect=on_connect)
        self._connections[key] = conn
        return conn

    # ---- BookTicker 流 ----

    async def subscribe_book_ticker(self, symbol: str, bus: EventBus):
        """订阅最优买卖价（最低延迟的价格更新）"""
        ws_sym = self._ws_symbol(symbol)
        stream = f"{ws_sym}@bookTicker"
        url = f"{self.base_url}/{stream}"
        key = f"book_ticker:{symbol}"

        async def on_message(raw):
            data = json.loads(raw)
            await bus.publish(f'book_ticker:{symbol}', {
                'symbol': symbol,
                'bid': float(data.get('b', 0)),
                'bid_qty': float(data.get('B', 0)),
                'ask': float(data.get('a', 0)),
                'ask_qty': float(data.get('A', 0)),
                'timestamp': data.get('T', 0),
            })

        conn = WSConnection(url, key, on_message)
        self._connections[key] = conn
        return conn

    # ---- 清算流 ----

    async def subscribe_liquidations(self, symbol: str, bus: EventBus):
        """订阅强平/清算事件"""
        ws_sym = self._ws_symbol(symbol)
        stream = f"{ws_sym}@forceOrder"
        url = f"{self.base_url}/{stream}"
        key = f"liquidations:{symbol}"

        async def on_message(raw):
            data = json.loads(raw)
            order = data.get('o', {})
            liq = Liquidation(
                symbol=symbol,
                exchange='binance',
                side=order.get('S', '').lower(),
                price=float(order.get('p', 0)),
                amount=float(order.get('q', 0)),
                timestamp=order.get('T', 0) / 1000,
            )
            await bus.publish(f'liquidation:{symbol}', liq)
            logger.info(f"[liquidation] {symbol} {liq.side} {liq.amount:.4f} @ {liq.price:.2f}")

        conn = WSConnection(url, key, on_message)
        self._connections[key] = conn
        return conn

    # ---- 组合流（单连接多 stream） ----

    async def subscribe_multi(self, streams: List[str], bus: EventBus):
        """
        订阅多个 stream（单连接，节省连接数）
        Binance 限制: 单连接最多 200 个 stream
        """
        stream_str = '/'.join(streams)
        url = f"{self.base_url}?streams={stream_str}"
        key = f"multi:{hashlib.md5(stream_str.encode()).hexdigest()[:8]}"

        async def on_message(raw):
            data = json.loads(raw)
            stream_name = data.get('stream', '')
            event_data = data.get('data', {})
            await bus.publish(f'combined:{stream_name}', event_data)

        conn = WSConnection(url, key, on_message)
        self._connections[key] = conn
        return conn

    # ---- 管理 ----

    async def stop_all(self):
        for conn in self._connections.values():
            await conn.stop()
        self._connections.clear()

    def get_stats(self) -> Dict:
        return {k: conn.stats for k, conn in self._connections.items()}


# ================================================================
# OKX WebSocket 适配器
# ================================================================

class OKXWSAdapter:
    """
    OKX WebSocket 适配器
    文档: https://www.okx.com/docs-v5/en/#websocket-api-public-channel
    """

    WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
    WS_URL_PRIVATE = "wss://ws.okx.com:8443/ws/v5/private"

    def __init__(self):
        self._connections: Dict[str, WSConnection] = {}

    def _inst_id(self, symbol: str) -> str:
        """BTC/USDT -> BTC-USDT-SWAP"""
        base, quote = symbol.split('/')
        return f"{base}-{quote}-SWAP"

    async def subscribe_kline(self, symbol: str, timeframe: str,
                               bus: EventBus):
        """订阅 OKX K 线"""
        inst_id = self._inst_id(symbol)
        key = f"okx:kline:{symbol}:{timeframe}"

        # OKX timeframe mapping
        tf_map = {'1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m',
                  '1h': '1H', '4h': '4H', '1d': '1D', '1w': '1W'}
        okx_tf = tf_map.get(timeframe, '1H')

        async def on_connect(ws):
            sub_msg = {
                "op": "subscribe",
                "args": [{"channel": f"candle{okx_tf}", "instId": inst_id}]
            }
            await ws.send(json.dumps(sub_msg))

        async def on_message(raw):
            data = json.loads(raw)
            if data.get('arg', {}).get('channel', '').startswith('candle'):
                for candle in data.get('data', []):
                    # OKX candle: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                    await bus.publish(f'kline:{symbol}:{timeframe}', {
                        'symbol': symbol,
                        'timeframe': timeframe,
                        'timestamp': int(candle[0]),
                        'open': float(candle[1]),
                        'high': float(candle[2]),
                        'low': float(candle[3]),
                        'close': float(candle[4]),
                        'volume': float(candle[5]),
                        'is_closed': candle[8] == '1',
                    })

        conn = WSConnection(self.WS_URL, key, on_message, on_connect=on_connect)
        self._connections[key] = conn
        return conn

    async def subscribe_trades(self, symbol: str, bus: EventBus):
        """订阅 OKX 逐笔成交"""
        inst_id = self._inst_id(symbol)
        key = f"okx:trades:{symbol}"

        async def on_connect(ws):
            sub_msg = {
                "op": "subscribe",
                "args": [{"channel": "trades", "instId": inst_id}]
            }
            await ws.send(json.dumps(sub_msg))

        async def on_message(raw):
            data = json.loads(raw)
            if data.get('arg', {}).get('channel') == 'trades':
                for t in data.get('data', []):
                    trade = Trade(
                        id=t.get('tradeId', ''),
                        symbol=symbol,
                        exchange='okx',
                        side=t.get('side', ''),
                        price=float(t.get('px', 0)),
                        amount=float(t.get('sz', 0)),
                        timestamp=int(t.get('ts', 0)) / 1000,
                    )
                    await bus.publish(f'trade:{symbol}', trade)

        conn = WSConnection(self.WS_URL, key, on_message, on_connect=on_connect)
        self._connections[key] = conn
        return conn

    async def subscribe_depth(self, symbol: str, bus: EventBus):
        """订阅 OKX OrderBook"""
        inst_id = self._inst_id(symbol)
        key = f"okx:depth:{symbol}"

        async def on_connect(ws):
            sub_msg = {
                "op": "subscribe",
                "args": [{"channel": "books5", "instId": inst_id}]
            }
            await ws.send(json.dumps(sub_msg))

        async def on_message(raw):
            data = json.loads(raw)
            if data.get('arg', {}).get('channel') == 'books5':
                for book_data in data.get('data', []):
                    book = OrderBook(
                        symbol=symbol,
                        exchange='okx',
                        bids=[OrderBookLevel(price=float(b[0]), amount=float(b[1]))
                              for b in book_data.get('bids', [])[:20]],
                        asks=[OrderBookLevel(price=float(a[0]), amount=float(a[1]))
                              for a in book_data.get('asks', [])[:20]],
                        timestamp=int(book_data.get('ts', 0)) / 1000,
                        last_update_id=int(book_data.get('seqId', 0)),
                    )
                    await bus.publish(f'orderbook:{symbol}', book)

        conn = WSConnection(self.WS_URL, key, on_message, on_connect=on_connect)
        self._connections[key] = conn
        return conn

    async def stop_all(self):
        for conn in self._connections.values():
            await conn.stop()
        self._connections.clear()

    def get_stats(self) -> Dict:
        return {k: conn.stats for k, conn in self._connections.items()}


# ================================================================
# K 线管理器（实时更新）
# ================================================================

class KlineManager:
    """管理实时 K 线数据，支持 WebSocket 推送更新"""

    def __init__(self, max_bars: int = 2000):
        self.max_bars = max_bars
        self._data: Dict[str, pd.DataFrame] = {}
        self._lock = asyncio.Lock()

    def _key(self, exchange: str, symbol: str, timeframe: str) -> str:
        return f"{exchange}:{symbol}:{timeframe}"

    async def initialize(self, exchange: str, symbol: str, timeframe: str,
                          df: pd.DataFrame):
        key = self._key(exchange, symbol, timeframe)
        async with self._lock:
            self._data[key] = df.tail(self.max_bars).copy()

    async def update_bar(self, exchange: str, symbol: str, timeframe: str,
                          timestamp: float, open_: float, high: float,
                          low: float, close: float, volume: float,
                          is_closed: bool = False):
        key = self._key(exchange, symbol, timeframe)
        ts = pd.Timestamp(timestamp, unit='ms')

        async with self._lock:
            if key not in self._data:
                self._data[key] = pd.DataFrame(
                    columns=['open', 'high', 'low', 'close', 'volume']
                )

            df = self._data[key]
            if ts in df.index:
                df.loc[ts, 'high'] = max(df.loc[ts, 'high'], high)
                df.loc[ts, 'low'] = min(df.loc[ts, 'low'], low)
                df.loc[ts, 'close'] = close
                df.loc[ts, 'volume'] = volume
            else:
                new_row = pd.DataFrame(
                    {'open': [open_], 'high': [high], 'low': [low],
                     'close': [close], 'volume': [volume]},
                    index=[ts]
                )
                self._data[key] = pd.concat([df, new_row]).tail(self.max_bars)

    def get(self, exchange: str, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        key = self._key(exchange, symbol, timeframe)
        df = self._data.get(key)
        return df.copy() if df is not None else None

    def latest_price(self, exchange: str, symbol: str,
                     timeframe: str = '1h') -> Optional[float]:
        key = self._key(exchange, symbol, timeframe)
        df = self._data.get(key)
        if df is not None and len(df) > 0:
            return float(df.iloc[-1]['close'])
        return None


# ================================================================
# OrderBook 管理器
# ================================================================

class OrderBookManager:
    def __init__(self, depth_levels: int = 20):
        self.depth_levels = depth_levels
        self._books: Dict[str, OrderBook] = {}

    def _key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def update(self, exchange: str, symbol: str, book: OrderBook):
        self._books[self._key(exchange, symbol)] = book

    def get(self, exchange: str, symbol: str) -> Optional[OrderBook]:
        return self._books.get(self._key(exchange, symbol))


# ================================================================
# 逐笔成交管理器
# ================================================================

class TradeStreamManager:
    def __init__(self, window_size: int = 1000):
        self.window_size = window_size
        self._trades: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size))
        self._stats: Dict[str, Dict] = {}

    def _key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def add_trade(self, trade: Trade):
        key = self._key(trade.exchange, trade.symbol)
        self._trades[key].append(trade)
        self._update_stats(key)

    def _update_stats(self, key: str):
        trades = list(self._trades[key])
        if not trades:
            return
        amounts = [t.amount for t in trades]
        buy_vol = sum(t.amount for t in trades if t.side == 'buy')
        sell_vol = sum(t.amount for t in trades if t.side == 'sell')

        self._stats[key] = {
            'count': len(trades),
            'avg_amount': float(np.mean(amounts)),
            'median_amount': float(np.median(amounts)),
            'buy_volume': buy_vol,
            'sell_volume': sell_vol,
            'buy_sell_ratio': buy_vol / sell_vol if sell_vol > 0 else float('inf'),
            'last_price': trades[-1].price,
            'last_ts': trades[-1].timestamp,
        }

    def get_stats(self, exchange: str, symbol: str) -> Optional[Dict]:
        return self._stats.get(self._key(exchange, symbol))

    def detect_whale_activity(self, exchange: str, symbol: str,
                               threshold_multiplier: float = 10.0,
                               window_seconds: int = 300) -> List[Dict]:
        key = self._key(exchange, symbol)
        trades = list(self._trades.get(key, []))
        if not trades:
            return []

        now = time.time()
        recent = [t for t in trades if now - t.timestamp < window_seconds]
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
                })
        return whales


# ================================================================
# 实时数据引擎 — 统一管理层
# ================================================================

class RealtimeDataEngine:
    """
    统一实时数据引擎

    管理所有交易所的 WebSocket 连接
    提供统一的数据查询和订阅接口
    策略引擎通过 EventBus 消费实时数据
    """

    def __init__(self):
        self.event_bus = EventBus()

        # K 线管理
        self.klines = KlineManager()
        self.orderbooks = OrderBookManager()
        self.trades = TradeStreamManager()

        # 交易所适配器
        self._adapters: Dict[str, Any] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []

        # 价格缓存（最低延迟）
        self._latest_prices: Dict[str, float] = {}

        # 注册内部事件处理
        self._register_handlers()

    def _register_handlers(self):
        """注册内部数据处理回调"""

        def on_kline(data):
            """同步处理 K 线更新"""
            asyncio.get_event_loop().create_task(
                self._handle_kline(data)
            )

        def on_orderbook(data):
            """同步处理 OrderBook 更新"""
            if isinstance(data, OrderBook):
                self.orderbooks.update(data.exchange, data.symbol, data)

        def on_trade(data):
            """同步处理逐笔成交"""
            if isinstance(data, Trade):
                self.trades.add_trade(data)
                # 更新最新价格
                self._latest_prices[f"{data.exchange}:{data.symbol}"] = data.price

        def on_book_ticker(data):
            """同步处理 BookTicker"""
            if isinstance(data, dict):
                key = f"{data.get('exchange', 'binance')}:{data['symbol']}"
                mid = (data['bid'] + data['ask']) / 2
                self._latest_prices[key] = mid

        self.event_bus.subscribe('kline', on_kline)
        self.event_bus.subscribe('orderbook', on_orderbook)
        self.event_bus.subscribe('trade', on_trade)
        self.event_bus.subscribe('book_ticker', on_book_ticker)

    async def _handle_kline(self, data):
        if not isinstance(data, dict):
            return
        await self.klines.update_bar(
            exchange='binance',  # TODO: dynamic
            symbol=data['symbol'],
            timeframe=data['timeframe'],
            timestamp=data['timestamp'],
            open_=data['open'],
            high=data['high'],
            low=data['low'],
            close=data['close'],
            volume=data['volume'],
            is_closed=data.get('is_closed', False),
        )

    def get_adapter(self, exchange_id: str, exchange_type: str = 'future'):
        """获取或创建交易所适配器"""
        if exchange_id not in self._adapters:
            if exchange_id == 'binance':
                self._adapters[exchange_id] = BinanceWSAdapter(exchange_type)
            elif exchange_id == 'okx':
                self._adapters[exchange_id] = OKXWSAdapter()
            else:
                raise ValueError(f"Unsupported exchange for WebSocket: {exchange_id}")
        return self._adapters[exchange_id]

    async def subscribe(self, exchange_id: str, symbol: str,
                         timeframe: str = '1h',
                         streams: List[str] = None,
                         exchange_type: str = 'future'):
        """
        订阅交易对的实时数据

        Args:
            exchange_id: 交易所 ID
            symbol: 交易对 (BTC/USDT)
            timeframe: K 线周期
            streams: 数据流列表 ['kline', 'trades', 'depth', 'book_ticker', 'liquidations']
            exchange_type: 'future' (永续) 或 'spot' (现货)
        """
        if streams is None:
            streams = ['kline', 'trades', 'depth', 'book_ticker']

        adapter = self.get_adapter(exchange_id, exchange_type)

        tasks = []

        if 'kline' in streams:
            conn = await adapter.subscribe_kline(symbol, timeframe, None, self.event_bus)
            tasks.append(conn)

        if 'trades' in streams:
            conn = await adapter.subscribe_trades(symbol, self.event_bus)
            tasks.append(conn)

        if 'depth' in streams:
            conn = await adapter.subscribe_depth(symbol, self.event_bus)
            tasks.append(conn)

        if 'book_ticker' in streams:
            conn = await adapter.subscribe_book_ticker(symbol, self.event_bus)
            tasks.append(conn)

        if 'liquidations' in streams:
            conn = await adapter.subscribe_liquidations(symbol, self.event_bus)
            tasks.append(conn)

        # 启动所有连接
        for conn in tasks:
            task = asyncio.create_task(conn.start())
            self._tasks.append(task)

        logger.info(f"Subscribed {exchange_id}:{symbol} streams={streams}")

    async def load_initial_data(self, exchange_id: str, symbol: str,
                                  timeframe: str = '1h', limit: int = 500):
        """通过 REST 加载初始历史数据"""
        import ccxt
        cls = getattr(ccxt, exchange_id)
        exchange = cls({'enableRateLimit': True})
        data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if data:
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df = df.astype(float)
            await self.klines.initialize(exchange_id, symbol, timeframe, df)
            logger.info(f"Initial data loaded: {exchange_id}:{symbol}:{timeframe} ({len(df)} bars)")

    # ---- 查询接口 ----

    def get_kline(self, exchange_id: str, symbol: str,
                  timeframe: str = '1h') -> Optional[pd.DataFrame]:
        return self.klines.get(exchange_id, symbol, timeframe)

    def get_orderbook(self, exchange_id: str, symbol: str) -> Optional[OrderBook]:
        return self.orderbooks.get(exchange_id, symbol)

    def get_latest_price(self, exchange_id: str, symbol: str) -> Optional[float]:
        return self._latest_prices.get(f"{exchange_id}:{symbol}")

    def get_trade_stats(self, exchange_id: str, symbol: str) -> Optional[Dict]:
        return self.trades.get_stats(exchange_id, symbol)

    def get_market_snapshot(self, exchange_id: str, symbol: str) -> Dict:
        """获取完整市场快照"""
        result = {
            'symbol': symbol,
            'exchange': exchange_id,
            'timestamp': datetime.now().isoformat(),
        }

        # 最新价格
        price = self.get_latest_price(exchange_id, symbol)
        if price:
            result['price'] = price

        # K 线
        df = self.get_kline(exchange_id, symbol, '1h')
        if df is not None and len(df) > 0:
            result['price'] = float(df.iloc[-1]['close'])
            result['volume_24h'] = float(df.tail(24)['volume'].sum()) if len(df) >= 24 else float(df['volume'].sum())

        # OrderBook
        ob = self.get_orderbook(exchange_id, symbol)
        if ob:
            result['orderbook'] = {
                'best_bid': ob.best_bid,
                'best_ask': ob.best_ask,
                'spread_pct': round(ob.spread_pct, 4),
                'mid_price': ob.mid_price,
                'imbalance': round(ob.imbalance_ratio(10), 3),
            }

        # 成交统计
        stats = self.get_trade_stats(exchange_id, symbol)
        if stats:
            result['trades'] = {
                'buy_sell_ratio': round(stats.get('buy_sell_ratio', 0), 3),
                'avg_amount': round(stats.get('avg_amount', 0), 4),
                'count': stats.get('count', 0),
            }

        return result

    async def stop(self):
        """停止所有数据流"""
        self._running = False
        for adapter in self._adapters.values():
            if hasattr(adapter, 'stop_all'):
                await adapter.stop_all()
        for task in self._tasks:
            task.cancel()
        logger.info("Realtime data engine stopped")


# ================================================================
# 全局单例
# ================================================================

# 在 asyncio 事件循环中初始化
_engine: Optional[RealtimeDataEngine] = None

def get_engine() -> RealtimeDataEngine:
    global _engine
    if _engine is None:
        _engine = RealtimeDataEngine()
    return _engine


# ================================================================
# 向后兼容的 DataManager 接口
# ================================================================

class DataManager:
    """
    向后兼容层 — 供 app.py 现有代码使用
    底层使用 RealtimeDataEngine 的 async 引擎
    """

    def __init__(self):
        self._engine = get_engine()
        # 兼容: 同步 API 包装
        self._sync_feeds: Dict[str, 'CompatFeed'] = {}

    def add_exchange(self, exchange_id: str,
                     api_key: str = '', api_secret: str = '') -> 'CompatFeed':
        if exchange_id not in self._sync_feeds:
            self._sync_feeds[exchange_id] = CompatFeed(exchange_id, self._engine)
        return self._sync_feeds[exchange_id]

    def get_feed(self, exchange_id: str) -> Optional['CompatFeed']:
        return self._sync_feeds.get(exchange_id)

    def subscribe_symbol(self, exchange_id: str, symbol: str,
                          timeframe: str = '1h',
                          streams: List[str] = None):
        """同步包装 — 实际启动 asyncio 任务"""
        feed = self.add_exchange(exchange_id)
        feed.subscribe(symbol, timeframe, streams)

    def get_kline(self, exchange_id: str, symbol: str,
                  timeframe: str = '1h') -> Optional[pd.DataFrame]:
        return self._engine.get_kline(exchange_id, symbol, timeframe)

    def get_orderbook(self, exchange_id: str, symbol: str):
        return self._engine.get_orderbook(exchange_id, symbol)

    def get_trade_stats(self, exchange_id: str, symbol: str):
        return self._engine.get_trade_stats(exchange_id, symbol)

    def get_latest_price(self, exchange_id: str, symbol: str,
                         timeframe: str = '1h'):
        return self._engine.get_latest_price(exchange_id, symbol)

    def get_market_snapshot(self, exchange_id: str, symbol: str) -> Dict:
        return self._engine.get_market_snapshot(exchange_id, symbol)

    def get_all_status(self) -> Dict:
        status = {}
        for eid, feed in self._sync_feeds.items():
            status[eid] = feed.get_status()
        return status

    def stop_all(self):
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self._engine.stop())
        else:
            loop.run_until_complete(self._engine.stop())


class CompatFeed:
    """兼容层 — 模拟旧 WebSocketFeed 接口"""

    def __init__(self, exchange_id: str, engine: RealtimeDataEngine):
        self.exchange_id = exchange_id
        self._engine = engine
        self._subscribed: Dict[str, List[str]] = {}

        # 兼容属性
        self.klines = engine.klines
        self.orderbook = engine.orderbooks
        self.trades = engine.trades

    def subscribe(self, symbol: str, timeframe: str = '1h',
                  streams: List[str] = None):
        if streams is None:
            streams = ['kline', 'trades', 'depth']
        self._subscribed[symbol] = streams

        # 在已有事件循环中启动
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self._engine.subscribe(self.exchange_id, symbol, timeframe, streams)
                )
            else:
                loop.run_until_complete(
                    self._engine.subscribe(self.exchange_id, symbol, timeframe, streams)
                )
        except RuntimeError:
            # 没有事件循环 — 创建一个后台线程运行
            import threading
            def _run():
                asyncio.run(self._engine.subscribe(self.exchange_id, symbol, timeframe, streams))
            t = threading.Thread(target=_run, daemon=True)
            t.start()

    # 兼容旧接口
    def start_kline_stream(self, symbol: str, timeframe: str = '1h'):
        self.subscribe(symbol, timeframe, ['kline'])

    def start_orderbook_stream(self, symbol: str, limit: int = 20):
        self.subscribe(symbol, '1h', ['depth'])

    def start_trades_stream(self, symbol: str):
        self.subscribe(symbol, '1h', ['trades'])

    def stop_all(self):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._engine.stop())
        except Exception:
            pass

    def get_status(self) -> Dict:
        return {
            'subscribed_symbols': list(self._subscribed.keys()),
            'exchange': self.exchange_id,
        }


# 模块级兼容单例
data_manager = DataManager()
