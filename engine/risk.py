"""
MyTradingPlatform — 高级风控系统
熔断机制 + 凯利公式仓位管理 + 完善追踪止损 + 极端行情检测

Phase 4: 风险控制层
- 全局/单策略熔断机制
- 凯利公式动态仓位
- 波动率自适应止损
- 黑天鹅检测
- 风险仪表盘
"""

import time
import logging
import threading
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)


# ================================================================
# 风险事件类型
# ================================================================

class RiskLevel(Enum):
    NORMAL = "normal"
    WARNING = "warning"
    DANGER = "danger"
    CRITICAL = "critical"
    CIRCUIT_BREAKER = "circuit_breaker"


class RiskEventType(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    MAX_DRAWDOWN = "max_drawdown"
    VOLATILITY_SPIKE = "volatility_spike"
    FLASH_CRASH = "flash_crash"
    API_LATENCY = "api_latency"
    CONSECUTIVE_LOSSES = "consecutive_losses"
    POSITION_LIMIT = "position_limit"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass
class RiskEvent:
    type: RiskEventType
    level: RiskLevel
    strategy_id: str = ""
    symbol: str = ""
    message: str = ""
    data: Dict = field(default_factory=dict)
    timestamp: float = 0.0
    action_taken: str = ""  # "none", "reduce", "close", "halt"


# ================================================================
# 波动率计算引擎
# ================================================================

class VolatilityEngine:
    """多维度波动率计算"""

    @staticmethod
    def historical_volatility(prices: np.ndarray, window: int = 20) -> float:
        """历史年化波动率"""
        if len(prices) < window + 1:
            return 0.0
        log_returns = np.diff(np.log(prices[-window - 1:]))
        return float(np.std(log_returns) * np.sqrt(365 * 24))  # 加密市场 24/7

    @staticmethod
    def parkinson_volatility(df: pd.DataFrame, window: int = 20) -> float:
        """Parkinson 波动率（基于 High-Low，比收盘价波动率更精确）"""
        if len(df) < window:
            return 0.0
        hl_ratio = np.log(df['high'] / df['low'])
        var = (hl_ratio ** 2).rolling(window).mean() / (4 * np.log(2))
        return float(np.sqrt(var.iloc[-1]) * np.sqrt(365 * 24)) if not np.isnan(var.iloc[-1]) else 0.0

    @staticmethod
    def ewma_volatility(prices: np.ndarray, span: int = 20, decay: float = 0.94) -> float:
        """EWMA 波动率（指数加权，对近期波动更敏感）"""
        if len(prices) < span + 1:
            return 0.0
        log_returns = np.diff(np.log(prices))
        # 使用 pandas ewm
        ret_series = pd.Series(log_returns)
        ewma_var = ret_series.ewm(span=span, adjust=False).var().iloc[-1]
        return float(np.sqrt(ewma_var) * np.sqrt(365 * 24)) if not np.isnan(ewma_var) else 0.0

    @staticmethod
    def detect_volatility_spike(prices: np.ndarray,
                                 current_window: int = 5,
                                 baseline_window: int = 60,
                                 threshold_multiplier: float = 3.0) -> Dict:
        """检测波动率突增（潜在黑天鹅信号）"""
        if len(prices) < baseline_window + current_window:
            return {'spike': False, 'ratio': 0}

        recent_ret = np.diff(np.log(prices[-current_window - 1:]))
        baseline_ret = np.diff(np.log(prices[-baseline_window - 1:-current_window]))

        recent_vol = np.std(recent_ret) if len(recent_ret) > 1 else 0
        baseline_vol = np.std(baseline_ret) if len(baseline_ret) > 1 else 0.001

        ratio = recent_vol / baseline_vol if baseline_vol > 0 else 0

        return {
            'spike': ratio > threshold_multiplier,
            'ratio': round(ratio, 2),
            'recent_vol': round(recent_vol * 100, 4),
            'baseline_vol': round(baseline_vol * 100, 4),
            'threshold': threshold_multiplier,
        }

    @staticmethod
    def implied_regime(prices: np.ndarray, window: int = 50) -> str:
        """判断市场波动状态"""
        if len(prices) < window:
            return 'unknown'

        vol = VolatilityEngine.historical_volatility(prices, window)
        # 加密市场典型阈值
        if vol < 0.3:
            return 'low_vol'      # 低波动，适合网格/均值回归
        elif vol < 0.6:
            return 'normal'       # 正常
        elif vol < 1.0:
            return 'high_vol'     # 高波动，适合趋势跟踪
        else:
            return 'extreme'      # 极端波动，应减少仓位


# ================================================================
# 凯利公式仓位管理器
# ================================================================

class KellyPositionSizer:
    """
    基于凯利公式计算最优仓位比例

    f* = (p * b - q) / b
    其中:
      p = 胜率
      b = 平均盈利/平均亏损比（赔率）
      q = 1 - p

    实际使用半凯利 (Half-Kelly) 降低波动
    """

    def __init__(self, kelly_fraction: float = 0.5,
                 min_position_pct: float = 1.0,
                 max_position_pct: float = 25.0):
        """
        kelly_fraction: 凯利系数 (0.5 = 半凯利, 更保守)
        min_position_pct: 最小仓位比例
        max_position_pct: 最大仓位比例
        """
        self.kelly_fraction = kelly_fraction
        self.min_pct = min_position_pct
        self.max_pct = max_position_pct
        self._trade_history: Dict[str, List[Dict]] = {}  # strategy_id -> trades

    def add_trade_result(self, strategy_id: str, pnl_pct: float, is_win: bool):
        """记录交易结果"""
        if strategy_id not in self._trade_history:
            self._trade_history[strategy_id] = []
        self._trade_history[strategy_id].append({
            'pnl_pct': pnl_pct,
            'is_win': is_win,
            'timestamp': time.time(),
        })
        # 只保留最近 200 笔
        if len(self._trade_history[strategy_id]) > 200:
            self._trade_history[strategy_id] = self._trade_history[strategy_id][-200:]

    def calculate(self, strategy_id: str,
                  override_win_rate: float = None,
                  override_payoff: float = None) -> Dict:
        """
        计算凯利最优仓位

        返回: {position_pct, win_rate, payoff_ratio, kelly_raw, kelly_adjusted, confidence}
        """
        trades = self._trade_history.get(strategy_id, [])

        if len(trades) < 10:
            # 样本不足，使用保守默认值
            return {
                'position_pct': self.min_pct,
                'win_rate': 0.5,
                'payoff_ratio': 1.0,
                'kelly_raw': 0.0,
                'kelly_adjusted': self.min_pct,
                'confidence': 'low',
                'sample_size': len(trades),
            }

        # 计算胜率
        wins = [t for t in trades if t['is_win']]
        losses = [t for t in trades if not t['is_win']]
        win_rate = override_win_rate or (len(wins) / len(trades) if trades else 0.5)

        # 计算赔率
        avg_win = np.mean([t['pnl_pct'] for t in wins]) if wins else 0.1
        avg_loss = abs(np.mean([t['pnl_pct'] for t in losses])) if losses else 0.1
        payoff = override_payoff or (avg_win / avg_loss if avg_loss > 0 else 1.0)

        # 凯利公式
        lose_rate = 1 - win_rate
        if payoff > 0:
            kelly_raw = (win_rate * payoff - lose_rate) / payoff
        else:
            kelly_raw = 0.0

        kelly_adjusted = kelly_raw * self.kelly_fraction

        # 限制在合理范围内
        position_pct = max(self.min_pct, min(self.max_pct, kelly_adjusted * 100))

        # 置信度
        if len(trades) >= 100:
            confidence = 'high'
        elif len(trades) >= 50:
            confidence = 'medium'
        else:
            confidence = 'low'

        return {
            'position_pct': round(position_pct, 2),
            'win_rate': round(win_rate * 100, 1),
            'payoff_ratio': round(payoff, 2),
            'kelly_raw': round(kelly_raw * 100, 2),
            'kelly_adjusted': round(kelly_adjusted * 100, 2),
            'confidence': confidence,
            'sample_size': len(trades),
        }


# ================================================================
# 高级追踪止损
# ================================================================

class AdvancedTrailingStop:
    """
    波动率自适应追踪止损
    根据 ATR 动态调整止损距离，而非固定百分比
    """

    def __init__(self, atr_multiplier: float = 2.0,
                 min_stop_pct: float = 1.0,
                 max_stop_pct: float = 10.0,
                 activation_pct: float = 1.0):
        """
        atr_multiplier: ATR 倍数作为止损距离
        min_stop_pct: 最小止损距离 (%)
        max_stop_pct: 最大止损距离 (%)
        activation_pct: 盈利达到多少%后激活追踪止损
        """
        self.atr_mult = atr_multiplier
        self.min_stop = min_stop_pct
        self.max_stop = max_stop_pct
        self.activation = activation_pct

    def check(self, entry_price: float, current_price: float,
              highest_price: float, lowest_price: float,
              side: str, atr: float = 0) -> Dict:
        """
        检查是否触发追踪止损

        返回: {triggered, stop_price, current_pnl_pct, trail_distance_pct}
        """
        if side == 'long':
            pnl_pct = (current_price / entry_price - 1) * 100

            # 未达到激活线
            if pnl_pct < self.activation:
                return {'triggered': False, 'pnl_pct': round(pnl_pct, 2),
                        'activated': False, 'stop_price': 0}

            # 计算 ATR 自适应止损距离
            if atr > 0:
                trail_pct = (atr * self.atr_mult) / current_price * 100
            else:
                trail_pct = self.min_stop * 2  # fallback
            trail_pct = max(self.min_stop, min(self.max_stop, trail_pct))

            # 追踪止损价 = 最高价 - 止损距离
            stop_price = highest_price * (1 - trail_pct / 100)

            triggered = current_price <= stop_price

            return {
                'triggered': triggered,
                'stop_price': round(stop_price, 2),
                'current_pnl_pct': round(pnl_pct, 2),
                'trail_distance_pct': round(trail_pct, 2),
                'activated': True,
                'peak_price': round(highest_price, 2),
                'drawdown_from_peak': round((highest_price - current_price) / highest_price * 100, 2),
            }

        else:  # short
            pnl_pct = (entry_price / current_price - 1) * 100

            if pnl_pct < self.activation:
                return {'triggered': False, 'pnl_pct': round(pnl_pct, 2),
                        'activated': False, 'stop_price': 0}

            if atr > 0:
                trail_pct = (atr * self.atr_mult) / current_price * 100
            else:
                trail_pct = self.min_stop * 2
            trail_pct = max(self.min_stop, min(self.max_stop, trail_pct))

            stop_price = lowest_price * (1 + trail_pct / 100)
            triggered = current_price >= stop_price

            return {
                'triggered': triggered,
                'stop_price': round(stop_price, 2),
                'current_pnl_pct': round(pnl_pct, 2),
                'trail_distance_pct': round(trail_pct, 2),
                'activated': True,
                'trough_price': round(lowest_price, 2),
                'rebound_from_trough': round((current_price - lowest_price) / lowest_price * 100, 2),
            }


# ================================================================
# 全局风控管理器 — 熔断机制
# ================================================================

class RiskManager:
    """
    全局风控管理器
    监控所有策略的实时风险，触发熔断保护
    """

    def __init__(self, config: Dict = None):
        cfg = config or {}

        # 全局风控参数
        self.global_max_drawdown_pct = cfg.get('global_max_drawdown_pct', 15.0)
        self.daily_loss_limit_pct = cfg.get('daily_loss_limit_pct', 5.0)
        self.max_consecutive_losses = cfg.get('max_consecutive_losses', 5)
        self.volatility_spike_threshold = cfg.get('volatility_spike_threshold', 3.0)
        self.max_api_latency_sec = cfg.get('max_api_latency_sec', 10.0)
        self.circuit_breaker_cooldown_sec = cfg.get('circuit_breaker_cooldown_sec', 300)

        # 状态
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_time: float = 0
        self._circuit_breaker_reason: str = ""
        self._daily_start_equity: float = 0
        self._peak_equity: float = 0
        self._current_equity: float = 0
        self._consecutive_losses: Dict[str, int] = {}  # strategy_id -> count
        self._risk_events: deque = deque(maxlen=500)
        self._strategy_risk: Dict[str, Dict] = {}
        self._halted_strategies: set = set()
        self._lock = threading.Lock()
        self._callbacks: List[callable] = []

        # 组件
        self.kelly = KellyPositionSizer()
        self.vol_engine = VolatilityEngine()
        self.trailing_stop = AdvancedTrailingStop()

    def register_callback(self, callback: callable):
        """注册风控事件回调"""
        self._callbacks.append(callback)

    def _emit_event(self, event: RiskEvent):
        event.timestamp = time.time()
        with self._lock:
            self._risk_events.append(event)
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Risk callback error: {e}")
        logger.warning(f"RISK EVENT [{event.level.value}] {event.type.value}: {event.message}")

    # ---- 实时检查 ----

    def check_position(self, strategy_id: str, symbol: str,
                       entry_price: float, current_price: float,
                       highest_price: float, lowest_price: float,
                       side: str, atr: float = 0,
                       equity: float = 0, capital: float = 0) -> Dict:
        """
        综合仓位风险检查

        返回: {action, events, position_pct, stop_info, risk_level}
        """
        result = {
            'action': 'hold',  # hold / reduce / close / halt
            'events': [],
            'risk_level': RiskLevel.NORMAL.value,
            'position_pct': 10,
            'stop_info': {},
        }

        # 1. 检查熔断
        if self._circuit_breaker_active:
            elapsed = time.time() - self._circuit_breaker_time
            if elapsed < self.circuit_breaker_cooldown_sec:
                result['action'] = 'halt'
                result['risk_level'] = RiskLevel.CIRCUIT_BREAKER.value
                result['reason'] = f"熔断中: {self._circuit_breaker_reason} (剩余{int(self.circuit_breaker_cooldown_sec - elapsed)}s)"
                return result
            else:
                # 冷却结束，解除熔断
                self._circuit_breaker_active = False
                logger.info("Circuit breaker cooldown ended, resuming")

        # 2. 策略是否被暂停
        if strategy_id in self._halted_strategies:
            result['action'] = 'halt'
            result['risk_level'] = RiskLevel.DANGER.value
            return result

        # 3. 追踪止损检查
        stop_info = self.trailing_stop.check(
            entry_price, current_price, highest_price, lowest_price, side, atr
        )
        result['stop_info'] = stop_info

        if stop_info.get('triggered'):
            event = RiskEvent(
                type=RiskEventType.TRAILING_STOP,
                level=RiskLevel.WARNING,
                strategy_id=strategy_id, symbol=symbol,
                message=f"追踪止损触发 @ {stop_info['stop_price']}",
                data=stop_info,
            )
            result['events'].append(event)
            result['action'] = 'close'

        # 4. 回撤检查
        if equity > 0 and capital > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100 if self._peak_equity > 0 else 0

            if drawdown_pct >= self.global_max_drawdown_pct:
                event = RiskEvent(
                    type=RiskEventType.MAX_DRAWDOWN,
                    level=RiskLevel.CRITICAL,
                    strategy_id=strategy_id, symbol=symbol,
                    message=f"最大回撤 {drawdown_pct:.1f}% 超限 {self.global_max_drawdown_pct}%",
                    data={'drawdown_pct': drawdown_pct},
                )
                result['events'].append(event)
                result['action'] = 'close'
                result['risk_level'] = RiskLevel.CRITICAL.value

                # 触发熔断
                self._trigger_circuit_breaker(
                    f"最大回撤 {drawdown_pct:.1f}% 超限",
                    RiskEventType.MAX_DRAWDOWN
                )

        # 5. 连续亏损检查
        losses = self._consecutive_losses.get(strategy_id, 0)
        if losses >= self.max_consecutive_losses:
            event = RiskEvent(
                type=RiskEventType.CONSECUTIVE_LOSSES,
                level=RiskLevel.DANGER,
                strategy_id=strategy_id, symbol=symbol,
                message=f"连续 {losses} 笔亏损，暂停策略",
            )
            result['events'].append(event)
            result['action'] = 'halt'
            result['risk_level'] = RiskLevel.DANGER.value
            self._halted_strategies.add(strategy_id)

        # 6. 动态仓位（凯利）
        kelly_result = self.kelly.calculate(strategy_id)
        result['position_pct'] = kelly_result['position_pct']
        result['kelly'] = kelly_result

        # 发出所有事件
        for event in result['events']:
            self._emit_event(event)

        return result

    def check_market_conditions(self, prices: np.ndarray,
                                 symbol: str = "") -> Dict:
        """
        市场环境风险检查
        检测波动率突增、闪崩等极端行情
        """
        result = {
            'risk_level': RiskLevel.NORMAL.value,
            'events': [],
            'regime': self.vol_engine.implied_regime(prices),
            'volatility': {},
        }

        if len(prices) < 20:
            return result

        # 波动率计算
        hist_vol = self.vol_engine.historical_volatility(prices)
        ewma_vol = self.vol_engine.ewma_volatility(prices)
        result['volatility'] = {
            'historical': round(hist_vol, 4),
            'ewma': round(ewma_vol, 4),
            'regime': result['regime'],
        }

        # 波动率突增检测
        spike_info = self.vol_engine.detect_volatility_spike(
            prices, threshold_multiplier=self.volatility_spike_threshold
        )
        result['vol_spike'] = spike_info

        if spike_info['spike']:
            # 计算短时间价格跌幅
            if len(prices) >= 5:
                short_drop = (prices[-1] / prices[-5] - 1) * 100
            else:
                short_drop = 0

            if short_drop < -5:
                # 闪崩
                event = RiskEvent(
                    type=RiskEventType.FLASH_CRASH,
                    level=RiskLevel.CRITICAL,
                    symbol=symbol,
                    message=f"闪崩检测: {short_drop:.1f}% 跌幅, 波动率 {spike_info['ratio']}x",
                    data={'price_drop_pct': short_drop, **spike_info},
                )
                result['risk_level'] = RiskLevel.CRITICAL.value
                result['action'] = 'halt_all'
            else:
                event = RiskEvent(
                    type=RiskEventType.VOLATILITY_SPIKE,
                    level=RiskLevel.WARNING,
                    symbol=symbol,
                    message=f"波动率突增: {spike_info['ratio']}x 基准",
                    data=spike_info,
                )
                result['risk_level'] = RiskLevel.WARNING.value

            result['events'].append(event)
            self._emit_event(event)

        return result

    def on_trade_result(self, strategy_id: str, pnl_pct: float):
        """记录交易结果，更新连续亏损计数和凯利数据"""
        is_win = pnl_pct > 0
        self.kelly.add_trade_result(strategy_id, pnl_pct, is_win)

        with self._lock:
            if is_win:
                self._consecutive_losses[strategy_id] = 0
            else:
                self._consecutive_losses[strategy_id] = self._consecutive_losses.get(strategy_id, 0) + 1

    def update_equity(self, equity: float):
        """更新当前权益"""
        with self._lock:
            self._current_equity = equity
            self._peak_equity = max(self._peak_equity, equity)
            if self._daily_start_equity == 0:
                self._daily_start_equity = equity

    def _trigger_circuit_breaker(self, reason: str, event_type: RiskEventType):
        """触发全局熔断"""
        self._circuit_breaker_active = True
        self._circuit_breaker_time = time.time()
        self._circuit_breaker_reason = reason

        event = RiskEvent(
            type=RiskEventType.CIRCUIT_BREAKER,
            level=RiskLevel.CIRCUIT_BREAKER,
            message=f"🔴 全局熔断触发: {reason}",
            data={'cooldown_sec': self.circuit_breaker_cooldown_sec, 'reason': reason},
            action_taken='halt_all',
        )
        self._emit_event(event)
        logger.critical(f"CIRCUIT BREAKER: {reason}")

    def release_circuit_breaker(self):
        """手动解除熔断"""
        self._circuit_breaker_active = False
        logger.info("Circuit breaker manually released")

    def resume_strategy(self, strategy_id: str):
        """手动恢复被暂停的策略"""
        self._halted_strategies.discard(strategy_id)
        self._consecutive_losses[strategy_id] = 0
        logger.info(f"Strategy {strategy_id} resumed manually")

    def get_risk_dashboard(self) -> Dict:
        """获取风控仪表盘数据"""
        with self._lock:
            events = list(self._risk_events)

        recent_events = events[-20:] if events else []
        event_counts = {}
        for e in events:
            key = e.type.value
            event_counts[key] = event_counts.get(key, 0) + 1

        return {
            'circuit_breaker': {
                'active': self._circuit_breaker_active,
                'reason': self._circuit_breaker_reason,
                'elapsed': time.time() - self._circuit_breaker_time if self._circuit_breaker_active else 0,
                'cooldown_remaining': max(0, self.circuit_breaker_cooldown_sec - (time.time() - self._circuit_breaker_time)) if self._circuit_breaker_active else 0,
            },
            'halted_strategies': list(self._halted_strategies),
            'consecutive_losses': dict(self._consecutive_losses),
            'equity': {
                'current': self._current_equity,
                'peak': self._peak_equity,
                'daily_start': self._daily_start_equity,
                'drawdown_pct': round((self._peak_equity - self._current_equity) / self._peak_equity * 100, 2) if self._peak_equity > 0 else 0,
                'daily_pnl_pct': round((self._current_equity - self._daily_start_equity) / self._daily_start_equity * 100, 2) if self._daily_start_equity > 0 else 0,
            },
            'limits': {
                'max_drawdown_pct': self.global_max_drawdown_pct,
                'daily_loss_limit_pct': self.daily_loss_limit_pct,
                'max_consecutive_losses': self.max_consecutive_losses,
                'vol_spike_threshold': self.volatility_spike_threshold,
            },
            'recent_events': [
                {
                    'type': e.type.value,
                    'level': e.level.value,
                    'message': e.message,
                    'strategy': e.strategy_id,
                    'symbol': e.symbol,
                    'time': datetime.fromtimestamp(e.timestamp).strftime('%H:%M:%S') if e.timestamp else '',
                    'action': e.action_taken,
                }
                for e in recent_events
            ],
            'event_counts': event_counts,
            'total_events': len(events),
        }

    def reset_daily(self):
        """每日重置（在 UTC 0 点调用）"""
        self._daily_start_equity = self._current_equity
        self._halted_strategies.clear()
        logger.info("Daily risk counters reset")


# ================================================================
# 全局单例
# ================================================================

risk_manager = RiskManager()
