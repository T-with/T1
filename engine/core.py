"""
MyTradingPlatform — 量化交易引擎
核心交易逻辑，独立于 Web 层
"""
import ccxt
import json
import time
import logging
import threading
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)


# ================================================================
# 数据模型
# ================================================================

class SignalType(Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
    HOLD = "hold"


@dataclass
class Trade:
    id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    side: str = ""           # buy/sell
    amount: float = 0.0
    price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    status: str = "open"     # open/closed
    opened_at: str = ""
    closed_at: str = ""


@dataclass
class Position:
    symbol: str = ""
    side: str = ""           # long/short
    size: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    unrealized_pnl: float = 0.0
    pnl_pct: float = 0.0
    opened_at: str = ""


@dataclass
class StrategyConfig:
    id: str = ""
    name: str = ""
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    type: str = "macd_cross"     # macd_cross/rsi_reversal/bollinger/dual_ma/grid
    params: Dict = field(default_factory=dict)
    capital: float = 10000.0
    leverage: int = 1
    position_size_pct: float = 10.0
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 6.0
    trailing_stop: bool = True
    trailing_stop_pct: float = 2.0
    max_drawdown_pct: float = 20.0
    status: str = "stopped"     # running/stopped/error
    paper: bool = True
    exchange_id: str = "binance"
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    created_at: str = ""
    updated_at: str = ""


# ================================================================
# 技术指标
# ================================================================

class Indicators:
    @staticmethod
    def sma(df, period, col='close'):
        return df[col].rolling(period).mean()

    @staticmethod
    def ema(df, period, col='close'):
        return df[col].ewm(span=period, adjust=False).mean()

    @staticmethod
    def rsi(df, period=14, col='close'):
        delta = df[col].diff()
        gain = delta.where(delta > 0, 0.0).ewm(com=period-1, min_periods=period).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(com=period-1, min_periods=period).mean()
        rs = gain / loss
        return 100 - 100 / (1 + rs)

    @staticmethod
    def macd(df, fast=12, slow=26, signal=9, col='close'):
        ema_f = df[col].ewm(span=fast, adjust=False).mean()
        ema_s = df[col].ewm(span=slow, adjust=False).mean()
        macd_line = ema_f - ema_s
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return macd_line, signal_line, macd_line - signal_line

    @staticmethod
    def bollinger(df, period=20, std_dev=2.0, col='close'):
        mid = df[col].rolling(period).mean()
        std = df[col].rolling(period).std()
        return mid + std_dev * std, mid, mid - std_dev * std

    @staticmethod
    def atr(df, period=14):
        hl = df['high'] - df['low']
        hc = (df['high'] - df['close'].shift()).abs()
        lc = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    @staticmethod
    def add_all(df):
        df = df.copy()
        ind = Indicators()
        df['sma_10'] = ind.sma(df, 10)
        df['sma_20'] = ind.sma(df, 20)
        df['sma_50'] = ind.sma(df, 50)
        df['ema_12'] = ind.ema(df, 12)
        df['ema_26'] = ind.ema(df, 26)
        df['rsi'] = ind.rsi(df)
        m, s, h = ind.macd(df)
        df['macd'], df['macd_signal'], df['macd_hist'] = m, s, h
        u, mid, l = ind.bollinger(df)
        df['bb_upper'], df['bb_middle'], df['bb_lower'] = u, mid, l
        df['atr'] = ind.atr(df)
        return df


# ================================================================
# 策略引擎
# ================================================================

class StrategyEngine:
    """策略信号生成"""

    STRATEGIES = {
        'macd_cross': 'MACD 金叉/死叉',
        'rsi_reversal': 'RSI 超买超卖',
        'bollinger_breakout': '布林带突破',
        'dual_ma': '双均线交叉',
        'grid': '网格交易',
    }

    @staticmethod
    def generate_signals(df: pd.DataFrame, strategy_type: str, params: Dict) -> List[Dict]:
        """生成交易信号"""
        if len(df) < 50:
            return []

        df = Indicators.add_all(df)

        if strategy_type == 'macd_cross':
            return StrategyEngine._macd_cross(df, params)
        elif strategy_type == 'rsi_reversal':
            return StrategyEngine._rsi_reversal(df, params)
        elif strategy_type == 'bollinger_breakout':
            return StrategyEngine._bollinger_breakout(df, params)
        elif strategy_type == 'dual_ma':
            return StrategyEngine._dual_ma(df, params)
        else:
            return []

    @staticmethod
    def _macd_cross(df, params):
        signals = []
        if len(df) < 2:
            return signals
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        if prev['macd'] <= prev['macd_signal'] and curr['macd'] > curr['macd_signal']:
            signals.append({'type': 'buy', 'price': curr['close'], 'confidence': 0.7})
        elif prev['macd'] >= prev['macd_signal'] and curr['macd'] < curr['macd_signal']:
            signals.append({'type': 'sell', 'price': curr['close'], 'confidence': 0.7})
        return signals

    @staticmethod
    def _rsi_reversal(df, params):
        signals = []
        if len(df) < 2:
            return signals
        oversold = params.get('oversold', 30)
        overbought = params.get('overbought', 70)
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        if prev['rsi'] < oversold and curr['rsi'] >= oversold:
            signals.append({'type': 'buy', 'price': curr['close'], 'confidence': 0.6})
        elif prev['rsi'] > overbought and curr['rsi'] <= overbought:
            signals.append({'type': 'sell', 'price': curr['close'], 'confidence': 0.6})
        return signals

    @staticmethod
    def _bollinger_breakout(df, params):
        signals = []
        if len(df) < 2:
            return signals
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        if prev['close'] <= prev['bb_upper'] and curr['close'] > curr['bb_upper']:
            signals.append({'type': 'buy', 'price': curr['close'], 'confidence': 0.65})
        elif prev['close'] >= prev['bb_lower'] and curr['close'] < curr['bb_lower']:
            signals.append({'type': 'sell', 'price': curr['close'], 'confidence': 0.65})
        return signals

    @staticmethod
    def _dual_ma(df, params):
        signals = []
        fast = params.get('fast_period', 10)
        slow = params.get('slow_period', 30)
        ma_type = params.get('ma_type', 'ema')
        ind = Indicators()
        if ma_type == 'ema':
            df['ma_f'] = ind.ema(df, fast)
            df['ma_s'] = ind.ema(df, slow)
        else:
            df['ma_f'] = ind.sma(df, fast)
            df['ma_s'] = ind.sma(df, slow)
        if len(df) < 2:
            return signals
        prev = df.iloc[-2]
        curr = df.iloc[-1]
        if prev['ma_f'] <= prev['ma_s'] and curr['ma_f'] > curr['ma_s']:
            signals.append({'type': 'buy', 'price': curr['close'], 'confidence': 0.6})
        elif prev['ma_f'] >= prev['ma_s'] and curr['ma_f'] < curr['ma_s']:
            signals.append({'type': 'sell', 'price': curr['close'], 'confidence': 0.6})
        return signals


# ================================================================
# 交易所客户端
# ================================================================

class ExchangeClient:
    def __init__(self, exchange_id='binance', api_key='', api_secret='', passphrase=''):
        cls = getattr(ccxt, exchange_id)
        params = {'enableRateLimit': True}
        if api_key:
            params['apiKey'] = api_key
            params['secret'] = api_secret
            if passphrase:
                params['password'] = passphrase
        self.exchange = cls(params)

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=500):
        data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df.astype(float)

    def fetch_ohlcv_range(self, symbol, timeframe, start, end):
        start_ts = int(pd.Timestamp(start).timestamp() * 1000)
        end_ts = int(pd.Timestamp(end).timestamp() * 1000)
        all_data = []
        since = start_ts
        tf_ms = self._tf_ms(timeframe)
        while since < end_ts:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not ohlcv:
                break
            all_data.extend(ohlcv)
            since = ohlcv[-1][0] + tf_ms
            time.sleep(self.exchange.rateLimit / 1000)
        if not all_data:
            return pd.DataFrame()
        df = pd.DataFrame(all_data, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df[~df.index.duplicated(keep='first')]
        return df.astype(float)

    def create_market_order(self, symbol, side, amount):
        return self.exchange.create_order(symbol, 'market', side, amount)

    def fetch_balance(self):
        return self.exchange.fetch_balance()

    def fetch_positions(self, symbols=None):
        try:
            return self.exchange.fetch_positions(symbols)
        except:
            return []

    @staticmethod
    def _tf_ms(tf):
        units = {'s':1000,'m':60000,'h':3600000,'d':86400000,'w':604800000}
        return int(tf[:-1]) * units[tf[-1]]


# ================================================================
# 回测引擎
# ================================================================

class BacktestEngine:
    @staticmethod
    def run(df, strategy_type, params, capital=10000, commission=0.0004,
            slippage=0.0005, leverage=1, position_pct=10, stop_loss_pct=3,
            take_profit_pct=6, trailing_stop=True, trailing_pct=2):
        df = Indicators.add_all(df)
        signals = []

        # 逐K线扫描生成信号
        for i in range(50, len(df)):
            window = df.iloc[:i+1]
            sigs = StrategyEngine.generate_signals(window, strategy_type, params)
            for s in sigs:
                s['index'] = i
                signals.append(s)

        # 模拟交易
        cash = capital
        position = None
        trades = []
        equity_curve = []
        max_equity = capital
        max_dd = 0

        for i in range(len(df)):
            price = df.iloc[i]['close']

            if position:
                position['current'] = price
                if position['side'] == 'long':
                    pnl_pct = (price / position['entry'] - 1) * 100
                    if position.get('highest', price) < price:
                        position['highest'] = price
                else:
                    pnl_pct = (position['entry'] / price - 1) * 100
                    if position.get('lowest', price) > price:
                        position['lowest'] = price

                # 止损
                if pnl_pct <= -stop_loss_pct:
                    exit_price = price * (1 - slippage) if position['side'] == 'long' else price * (1 + slippage)
                    pnl = (exit_price - position['entry']) * position['size'] if position['side'] == 'long' else (position['entry'] - exit_price) * position['size']
                    fee = abs(pnl) * commission if pnl > 0 else position['size'] * exit_price * commission
                    cash += pnl - fee
                    trades.append({**position, 'exit_price': exit_price, 'pnl': pnl - fee, 'pnl_pct': pnl_pct, 'reason': 'stop_loss'})
                    position = None

                # 止盈
                elif pnl_pct >= take_profit_pct:
                    exit_price = price * (1 - slippage) if position['side'] == 'long' else price * (1 + slippage)
                    pnl = (exit_price - position['entry']) * position['size'] if position['side'] == 'long' else (position['entry'] - exit_price) * position['size']
                    fee = abs(pnl) * commission
                    cash += pnl - fee
                    trades.append({**position, 'exit_price': exit_price, 'pnl': pnl - fee, 'pnl_pct': pnl_pct, 'reason': 'take_profit'})
                    position = None

                # 追踪止损
                elif trailing_stop and pnl_pct > 0:
                    if position['side'] == 'long':
                        pullback = (position.get('highest', price) - price) / position.get('highest', price) * 100
                        if pullback >= trailing_pct:
                            exit_price = price * (1 - slippage)
                            pnl = (exit_price - position['entry']) * position['size']
                            fee = abs(pnl) * commission
                            cash += pnl - fee
                            trades.append({**position, 'exit_price': exit_price, 'pnl': pnl - fee, 'pnl_pct': pnl_pct, 'reason': 'trailing_stop'})
                            position = None

            # 检查信号
            for sig in signals:
                if sig['index'] == i and position is None:
                    entry = price * (1 + slippage) if sig['type'] == 'buy' else price * (1 - slippage)
                    size = (cash * position_pct / 100 * leverage) / entry
                    fee = size * entry * commission
                    cash -= fee
                    position = {
                        'side': 'long' if sig['type'] == 'buy' else 'short',
                        'entry': entry, 'size': size, 'open_bar': i,
                        'highest': entry, 'lowest': entry,
                    }

            # 最后一根K线平仓
            if position and i == len(df) - 1:
                exit_price = price * (1 - slippage) if position['side'] == 'long' else price * (1 + slippage)
                pnl = (exit_price - position['entry']) * position['size'] if position['side'] == 'long' else (position['entry'] - exit_price) * position['size']
                fee = abs(pnl) * commission if pnl > 0 else position['size'] * exit_price * commission
                cash += pnl - fee
                trades.append({**position, 'exit_price': exit_price, 'pnl': pnl - fee, 'pnl_pct': (pnl / (position['entry'] * position['size'])) * 100, 'reason': 'end'})
                position = None

            eq = cash
            if position:
                if position['side'] == 'long':
                    eq += (price - position['entry']) * position['size']
                else:
                    eq += (position['entry'] - price) * position['size']
            equity_curve.append(eq)
            max_equity = max(max_equity, eq)
            dd = (max_equity - eq) / max_equity * 100 if max_equity > 0 else 0
            max_dd = max(max_dd, dd)

        # 统计
        wins = [t for t in trades if t.get('pnl', 0) > 0]
        losses = [t for t in trades if t.get('pnl', 0) <= 0]
        total_profit = sum(t['pnl'] for t in wins) if wins else 0
        total_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0

        returns = pd.Series(equity_curve).pct_change().dropna()
        sharpe = float(returns.mean() / returns.std() * np.sqrt(365*24)) if len(returns) > 1 and returns.std() > 0 else 0

        return {
            'initial_capital': capital,
            'final_capital': cash,
            'total_return_pct': (cash / capital - 1) * 100,
            'total_trades': len(trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': len(wins) / len(trades) * 100 if trades else 0,
            'avg_win_pct': np.mean([t['pnl_pct'] for t in wins]) if wins else 0,
            'avg_loss_pct': np.mean([t['pnl_pct'] for t in losses]) if losses else 0,
            'profit_factor': total_profit / total_loss if total_loss > 0 else 0,
            'max_drawdown_pct': max_dd,
            'sharpe_ratio': sharpe,
            'trades': [
                {
                    'side': t['side'],
                    'entry_price': round(t['entry'], 2),
                    'exit_price': round(t.get('exit_price', 0), 2),
                    'pnl': round(t.get('pnl', 0), 2),
                    'pnl_pct': round(t.get('pnl_pct', 0), 2),
                    'reason': t.get('reason', ''),
                }
                for t in trades
            ],
            'equity_curve': [round(e, 2) for e in equity_curve],
        }


# ================================================================
# 实盘执行器
# ================================================================

class LiveTrader:
    """实盘交易执行器 — 后台线程运行"""

    def __init__(self):
        self._strategies: Dict[str, Dict] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, config: StrategyConfig) -> bool:
        sid = config.id
        with self._lock:
            if sid in self._threads and self._threads[sid].is_alive():
                return False

            stop_event = threading.Event()
            self._stop_events[sid] = stop_event
            self._strategies[sid] = {
                'config': config,
                'positions': {},
                'trades': [],
                'equity': config.capital,
                'last_signal': 'hold',
                'last_update': datetime.now().isoformat(),
                'errors': [],
            }

            t = threading.Thread(target=self._run_loop, args=(sid,), daemon=True)
            self._threads[sid] = t
            t.start()

            logger.info(f"Strategy {config.name} started")
            return True

    def stop(self, sid: str):
        with self._lock:
            if sid in self._stop_events:
                self._stop_events[sid].set()
            if sid in self._strategies:
                self._strategies[sid]['config'].status = 'stopped'

    def get_status(self, sid: str) -> Optional[Dict]:
        return self._strategies.get(sid)

    def get_all_status(self) -> Dict:
        return self._strategies

    def _run_loop(self, sid: str):
        state = self._strategies[sid]
        config = state['config']
        config.status = 'running'

        stop_event = self._stop_events[sid]
        interval_map = {'1m': 10, '5m': 30, '15m': 60, '30m': 120, '1h': 300, '4h': 900, '1d': 3600}
        interval = interval_map.get(config.timeframe, 300)

        while not stop_event.is_set():
            try:
                client = ExchangeClient(
                    config.exchange_id, config.api_key, config.api_secret, config.passphrase
                )
                df = client.fetch_ohlcv(config.symbol, config.timeframe, limit=200)
                if df.empty:
                    stop_event.wait(interval)
                    continue

                signals = StrategyEngine.generate_signals(df, config.type, config.params)
                current_price = float(df.iloc[-1]['close'])

                for sig in signals[-1:]:
                    if sig['type'] == 'buy' and not state['positions']:
                        if not config.paper and config.api_key:
                            try:
                                amount_usdt = config.capital * config.position_size_pct / 100 * config.leverage
                                size = amount_usdt / current_price
                                order = client.create_market_order(config.symbol, 'buy', size)
                                logger.info(f"Order placed: {order}")
                            except Exception as e:
                                state['errors'].append(str(e))
                                logger.error(f"Order failed: {e}")
                                continue

                        state['positions'][config.symbol] = {
                            'side': 'long', 'size': amount_usdt / current_price,
                            'entry_price': current_price, 'current_price': current_price,
                            'opened_at': datetime.now().isoformat(),
                        }
                        state['last_signal'] = 'buy'
                        state['trades'].append({
                            'type': 'open_long', 'price': current_price,
                            'time': datetime.now().isoformat(),
                        })

                    elif sig['type'] == 'sell' and config.symbol in state['positions']:
                        pos = state['positions'].pop(config.symbol, {})
                        pnl = (current_price - pos.get('entry_price', current_price)) * pos.get('size', 0)
                        state['equity'] += pnl
                        state['last_signal'] = 'sell'
                        state['trades'].append({
                            'type': 'close_long', 'price': current_price,
                            'pnl': round(pnl, 2), 'time': datetime.now().isoformat(),
                        })

                # 风控检查
                for sym in list(state['positions'].keys()):
                    pos = state['positions'][sym]
                    pos['current_price'] = current_price
                    pnl_pct = (current_price / pos['entry_price'] - 1) * 100
                    pos['pnl_pct'] = round(pnl_pct, 2)

                    if pnl_pct <= -config.stop_loss_pct:
                        if not config.paper and config.api_key:
                            try:
                                client.create_market_order(config.symbol, 'sell', pos['size'])
                            except:
                                pass
                        state['positions'].pop(sym)
                        state['trades'].append({
                            'type': 'stop_loss', 'price': current_price,
                            'pnl_pct': round(pnl_pct, 2), 'time': datetime.now().isoformat(),
                        })

                    elif pnl_pct >= config.take_profit_pct:
                        if not config.paper and config.api_key:
                            try:
                                client.create_market_order(config.symbol, 'sell', pos['size'])
                            except:
                                pass
                        state['positions'].pop(sym)
                        state['trades'].append({
                            'type': 'take_profit', 'price': current_price,
                            'pnl_pct': round(pnl_pct, 2), 'time': datetime.now().isoformat(),
                        })

                state['last_update'] = datetime.now().isoformat()

            except Exception as e:
                logger.error(f"Strategy {sid} error: {e}")
                state['errors'].append(f"{datetime.now():%H:%M:%S} {e}")

            stop_event.wait(interval)

        logger.info(f"Strategy {sid} stopped")
