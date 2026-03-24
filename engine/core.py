"""
MyTradingPlatform — 量化交易引擎
核心交易逻辑，独立于 Web 层
"""
import ccxt
import json
import time
import logging
import threading
import hashlib
import os
import pickle
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, fields as dc_fields
from typing import Optional, Dict, List, Any, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)

# ================================================================
# 日志文件
# ================================================================
log_file = BASE_DIR / 'logs' / 'platform.log'
log_file.parent.mkdir(exist_ok=True)
_file_handler = logging.FileHandler(log_file, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
logging.getLogger().addHandler(_file_handler)


# ================================================================
# API 密钥加密
# ================================================================
_KEY_FILE = DATA_DIR / '.enc_key'

def _get_encryption_key() -> bytes:
    """加载或生成 Fernet 加密密钥"""
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    # 生成新密钥（仅第一次）
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    os.chmod(_KEY_FILE, 0o600)
    return key

def encrypt_secret(plaintext: str) -> str:
    """加密敏感字符串"""
    if not plaintext:
        return ''
    from cryptography.fernet import Fernet
    key = _get_encryption_key()
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()

def decrypt_secret(ciphertext: str) -> str:
    """解密敏感字符串"""
    if not ciphertext:
        return ''
    from cryptography.fernet import Fernet
    key = _get_encryption_key()
    f = Fernet(key)
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        # 兼容：如果解密失败，可能是旧的明文格式，直接返回
        return ciphertext

def encrypt_exchange_config(data: dict) -> dict:
    """加密交易所配置中的敏感字段"""
    data = dict(data)
    for field in ('api_key', 'api_secret', 'passphrase'):
        if data.get(field):
            data[field] = f"enc:{encrypt_secret(data[field])}"
    return data

def decrypt_exchange_config(data: dict) -> dict:
    """解密交易所配置中的敏感字段"""
    data = dict(data)
    for field in ('api_key', 'api_secret', 'passphrase'):
        val = data.get(field, '')
        if val.startswith('enc:'):
            data[field] = decrypt_secret(val[4:])
        # 否则保持原样（旧明文兼容）
    return data

def redact_secrets(data: dict) -> dict:
    """脱敏 — 返回给前端时隐藏密钥"""
    data = dict(data)
    for field in ('api_key', 'api_secret', 'passphrase'):
        val = data.get(field, '')
        if val:
            # 显示前4后4位
            if val.startswith('enc:'):
                raw = decrypt_secret(val[4:])
            else:
                raw = val
            if len(raw) > 8:
                data[field] = raw[:4] + '****' + raw[-4:]
            else:
                data[field] = '****'
    return data

def decrypt_strategy_secrets(config: dict) -> dict:
    """解密策略配置中的敏感字段（用于实盘启动时）"""
    config = dict(config)
    for field in ('api_key', 'api_secret', 'passphrase'):
        val = config.get(field, '')
        if isinstance(val, str) and val.startswith('enc:'):
            config[field] = decrypt_secret(val[4:])
    return config


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

# 延迟计算合法字段（dataclass 要求前向声明后才可用）
_STRATEGY_FIELD_NAMES = {f.name for f in dc_fields(StrategyConfig)}

def filter_strategy_config(data: dict) -> dict:
    """过滤字典，只保留 StrategyConfig 的合法字段"""
    return {k: v for k, v in data.items() if k in _STRATEGY_FIELD_NAMES}


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
        'ai_multi_factor': 'AI 多因子 (XGBoost)',
        'lstm': 'LSTM 深度学习',
        'transformer': 'Transformer 注意力',
        'rl_ppo': '强化学习 PPO',
    }

    @staticmethod
    def generate_signals(df: pd.DataFrame, strategy_type: str, params: Dict) -> List[Dict]:
        """生成交易信号"""
        if len(df) < 50:
            return []

        df = Indicators.add_all(df)

        if strategy_type == 'ai_multi_factor':
            return AIMultiFactorStrategy.generate_signals(df, params)
        elif strategy_type in ('lstm', 'transformer'):
            return StrategyEngine._dl_signal(df, strategy_type, params)
        elif strategy_type == 'rl_ppo':
            return StrategyEngine._rl_signal(df, params)
        elif strategy_type == 'macd_cross':
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

    @staticmethod
    def _dl_signal(df, model_type, params):
        """LSTM/Transformer 深度学习信号生成"""
        signals = []
        try:
            from engine.models import model_manager
            result = model_manager.predict(df, model_type)
            if result.get('status') != 'ok':
                return signals
            curr = df.iloc[-1]
            if result['signal'] == 'buy':
                signals.append({
                    'type': 'buy',
                    'price': curr['close'],
                    'confidence': result['confidence'] / 100,
                })
            elif result['signal'] == 'sell':
                signals.append({
                    'type': 'sell',
                    'price': curr['close'],
                    'confidence': result['confidence'] / 100,
                })
        except Exception as e:
            logger.error(f"DL signal error ({model_type}): {e}")
        return signals

    @staticmethod
    def _rl_signal(df, params):
        """强化学习 PPO 信号生成"""
        signals = []
        try:
            from engine.rl import rl_manager
            result = rl_manager.predict(df, params.get('symbol', 'BTC/USDT'), params.get('timeframe', '1h'))
            if result.get('status') != 'ok':
                return signals
            curr = df.iloc[-1]
            if result['action'] == 1:  # buy
                signals.append({
                    'type': 'buy',
                    'price': curr['close'],
                    'confidence': result['probabilities']['buy'],
                })
            elif result['action'] == 2:  # sell
                signals.append({
                    'type': 'sell',
                    'price': curr['close'],
                    'confidence': result['probabilities']['sell'],
                })
        except Exception as e:
            logger.error(f"RL signal error: {e}")
        return signals


# ================================================================
# AI 多因子策略 — XGBoost 自动因子挖掘 + Walk-Forward
# ================================================================

class AIMultiFactorStrategy:
    """
    AI 多因子策略

    核心逻辑：
    1. 因子工程：从 OHLCV 自动生成 50+ 技术因子（动量/波动率/量价/趋势）
    2. 标签：下一根 K 线收益 > 阈值 → 买入(1)，否则卖出(0)
    3. 模型：XGBoost，walk-forward 滚动训练，避免未来数据泄露
    4. 信号：模型预测买入概率 > 阈值 → 开仓，< 阈值 → 平仓

    可调参数：
    - train_window: 训练窗口大小（默认 500 根 K 线）
    - retrain_interval: 每隔多少根 K 线重新训练（默认 50）
    - buy_threshold: 买入概率阈值（默认 0.6）
    - sell_threshold: 卖出概率阈值（默认 0.4）
    - target_return: 目标收益率阈值，下一根收益 > 此值才算正样本（默认 0.002 = 0.2%）
    - n_estimators: XGBoost 树数量（默认 100）
    - max_depth: XGBoost 最大深度（默认 5）
    """

    # 缓存已训练的模型
    _model_cache = {}

    @staticmethod
    def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
        """计算全部因子（特征工程）"""
        f = df.copy()
        close = f['close']
        high = f['high']
        low = f['low']
        volume = f['volume']

        # === 动量因子 ===
        for p in [5, 10, 20, 60]:
            f[f'mom_{p}'] = close / close.shift(p) - 1  # N期收益率

        # === 波动率因子 ===
        ret = close.pct_change()
        for p in [5, 10, 20, 60]:
            f[f'vol_{p}'] = ret.rolling(p).std()  # 滚动波动率
        f['vol_ratio_5_20'] = f['vol_5'] / f['vol_20'].replace(0, np.nan)  # 短/长期波动比

        # === 技术指标因子 ===
        # RSI
        f['rsi_14'] = Indicators.rsi(f, 14)
        f['rsi_7'] = Indicators.rsi(f, 7)
        f['rsi_21'] = Indicators.rsi(f, 21)
        # MACD
        macd_line, sig_line, hist = Indicators.macd(f)
        f['macd_hist'] = hist
        f['macd_hist_slope'] = hist - hist.shift(3)  # MACD柱状图斜率
        # 布林带宽度
        upper, mid, lower = Indicators.bollinger(f)
        f['bb_width'] = (upper - lower) / mid
        f['bb_position'] = (close - lower) / (upper - lower).replace(0, np.nan)  # 价格在BB中的位置
        # ATR
        f['atr'] = Indicators.atr(f)
        f['atr_ratio'] = f['atr'] / close  # ATR/价格比

        # === 趋势因子 ===
        for p in [10, 20, 50]:
            sma = close.rolling(p).mean()
            ema = close.ewm(span=p, adjust=False).mean()
            f[f'sma_dist_{p}'] = (close - sma) / sma  # 距SMA的距离
            f[f'ema_dist_{p}'] = (close - ema) / ema  # 距EMA的距离

        # === 量价因子 ===
        vol_sma_20 = volume.rolling(20).mean()
        f['vol_ratio'] = volume / vol_sma_20.replace(0, np.nan)  # 量比
        f['price_vol_corr'] = ret.rolling(20).corr(volume.pct_change())  # 价量相关性
        # OBV 斜率
        obv = (np.sign(ret) * volume).cumsum()
        f['obv_slope_10'] = (obv - obv.shift(10)) / 10

        # === K线形态因子 ===
        f['body_ratio'] = (close - f['open']).abs() / (high - low).replace(0, np.nan)  # 实体占比
        f['upper_shadow'] = (high - close.clip(lower=f['open'])) / (high - low).replace(0, np.nan)
        f['lower_shadow'] = (f['open'].clip(upper=close) - low) / (high - low).replace(0, np.nan)

        # === 均值回归因子 ===
        for p in [20, 60]:
            sma = close.rolling(p).mean()
            std = close.rolling(p).std()
            f[f'zscore_{p}'] = (close - sma) / std.replace(0, np.nan)  # Z-score

        # === 时间因子 ===
        if hasattr(f.index, 'hour'):
            f['hour_sin'] = np.sin(2 * np.pi * f.index.hour / 24)
            f['hour_cos'] = np.cos(2 * np.pi * f.index.hour / 24)
        if hasattr(f.index, 'dayofweek'):
            f['dow_sin'] = np.sin(2 * np.pi * f.index.dayofweek / 7)
            f['dow_cos'] = np.cos(2 * np.pi * f.index.dayofweek / 7)

        return f

    @staticmethod
    def get_factor_columns(df: pd.DataFrame) -> list:
        """获取所有因子列名"""
        exclude = {'open', 'high', 'low', 'close', 'volume', 'timestamp'}
        exclude.update(c for c in df.columns if c.startswith(('sma_', 'ema_', 'bb_', 'macd_')) and c not in ('macd_hist', 'macd_hist_slope'))
        return [c for c in df.columns if c not in exclude and not c.startswith('_')]

    @staticmethod
    def _train_model(X_train, y_train, params):
        """训练 XGBoost 模型"""
        from xgboost import XGBClassifier

        n_estimators = params.get('n_estimators', 100)
        max_depth = params.get('max_depth', 5)
        learning_rate = params.get('learning_rate', 0.1)

        model = XGBClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=learning_rate,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=1.0,
            use_label_encoder=False,
            eval_metric='logloss',
            random_state=42,
            n_jobs=1,
        )
        model.fit(X_train, y_train)
        return model

    @staticmethod
    def generate_signals(df: pd.DataFrame, params: Dict) -> List[Dict]:
        """Walk-Forward AI 多因子信号生成（实盘用）"""
        try:
            return AIMultiFactorStrategy._generate_impl(df, params)
        except Exception as e:
            logger.error(f"AI MultiFactor error: {e}", exc_info=True)
            return []

    @staticmethod
    def _generate_impl(df: pd.DataFrame, params: Dict) -> List[Dict]:
        train_window = params.get('train_window', 500)
        buy_threshold = params.get('buy_threshold', 0.6)
        sell_threshold = params.get('sell_threshold', 0.4)
        target_return = params.get('target_return', 0.002)

        if len(df) < train_window + 60:
            return []

        # 计算因子
        fdf = AIMultiFactorStrategy.compute_factors(df)
        factor_cols = AIMultiFactorStrategy.get_factor_columns(fdf)

        # 构建训练数据：用前 train_window 根预测最新一根
        train_df = fdf.iloc[-(train_window + 1):-1].copy()
        train_df = train_df[factor_cols + ['close']].dropna()
        if len(train_df) < 100:
            return []

        # 标签：下一根收益 > target_return → 1
        train_df['target'] = (train_df['close'].shift(-1) / train_df['close'] - 1 > target_return).astype(int)
        train_df = train_df.dropna(subset=['target'])

        X_train = train_df[factor_cols].values
        y_train = train_df['target'].values

        if len(np.unique(y_train)) < 2:
            return []

        # 训练
        model = AIMultiFactorStrategy._train_model(X_train, y_train, params)

        # 预测最新一根
        latest = fdf.iloc[[-1]][factor_cols].dropna(axis=1, how='all')
        # 对齐列
        for col in factor_cols:
            if col not in latest.columns:
                latest[col] = 0
        latest = latest[factor_cols]

        if latest.isna().all(axis=1).iloc[0]:
            return []

        # 填充 NaN 为 0（缺失因子）
        latest = latest.fillna(0)
        prob = model.predict_proba(latest.values)[0]
        buy_prob = prob[1] if len(prob) > 1 else prob[0]
        current_price = float(df.iloc[-1]['close'])

        signals = []
        if buy_prob >= buy_threshold:
            signals.append({
                'type': 'buy',
                'price': current_price,
                'confidence': round(float(buy_prob), 3),
            })
        elif buy_prob <= sell_threshold:
            signals.append({
                'type': 'sell',
                'price': current_price,
                'confidence': round(float(1 - buy_prob), 3),
            })

        return signals

    @staticmethod
    def vectorized_signals(df: pd.DataFrame, params: Dict) -> Tuple[np.ndarray, np.ndarray]:
        """
        向量化信号生成（回测用）
        Walk-Forward: 每隔 retrain_interval 根 K 线重新训练模型
        """
        from xgboost import XGBClassifier

        train_window = params.get('train_window', 500)
        retrain_interval = params.get('retrain_interval', 50)
        buy_threshold = params.get('buy_threshold', 0.6)
        sell_threshold = params.get('sell_threshold', 0.4)
        target_return = params.get('target_return', 0.002)
        n_estimators = params.get('n_estimators', 100)
        max_depth = params.get('max_depth', 5)
        learning_rate = params.get('learning_rate', 0.1)

        n = len(df)
        buy = np.zeros(n, dtype=bool)
        sell = np.zeros(n, dtype=bool)

        # 计算全部因子
        fdf = AIMultiFactorStrategy.compute_factors(df)
        factor_cols = AIMultiFactorStrategy.get_factor_columns(fdf)

        # 构建标签（全部）
        fdf['_target'] = (fdf['close'].shift(-1) / fdf['close'] - 1 > target_return).astype(int)

        # 删除有 NaN 的行
        valid_mask = fdf[factor_cols].notna().all(axis=1) & fdf['_target'].notna()
        fdf_clean = fdf.loc[valid_mask].copy()

        if len(fdf_clean) < train_window + 100:
            return buy, sell

        X_all = fdf_clean[factor_cols].values
        y_all = fdf_clean['_target'].values.astype(int)
        indices = fdf_clean.index

        # Walk-Forward
        model = None
        last_train_end = -1

        for i in range(train_window, len(fdf_clean)):
            # 需要重新训练
            if model is None or (i - last_train_end) >= retrain_interval:
                train_start = max(0, i - train_window)
                X_tr = X_all[train_start:i]
                y_tr = y_all[train_start:i]

                if len(np.unique(y_tr)) < 2:
                    continue

                model = XGBClassifier(
                    n_estimators=n_estimators,
                    max_depth=max_depth,
                    learning_rate=learning_rate,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    reg_alpha=0.1,
                    reg_lambda=1.0,
                    use_label_encoder=False,
                    eval_metric='logloss',
                    random_state=42,
                    n_jobs=1,
                )
                model.fit(X_tr, y_tr)
                last_train_end = i

            if model is None:
                continue

            # 预测当前点
            x_cur = X_all[i].reshape(1, -1)
            if np.isnan(x_cur).any():
                continue

            prob = model.predict_proba(x_cur)[0]
            buy_prob = prob[1] if len(prob) > 1 else prob[0]

            # 映射回原始 df 的索引
            orig_idx = df.index.get_loc(indices[i])
            if buy_prob >= buy_threshold:
                buy[orig_idx] = True
            elif buy_prob <= sell_threshold:
                sell[orig_idx] = True

        # 过滤前 train_window
        buy[:train_window] = False
        sell[:train_window] = False

        return buy, sell

    @staticmethod
    def analyze(df: pd.DataFrame, params: Dict) -> Dict:
        """
        完整 AI 分析报告 — 用于仪表盘
        返回：模型状态、因子重要性、预测概率、市场状态、信号详情
        """
        from xgboost import XGBClassifier

        train_window = params.get('train_window', 500)
        buy_threshold = params.get('buy_threshold', 0.6)
        sell_threshold = params.get('sell_threshold', 0.4)
        target_return = params.get('target_return', 0.002)
        n_estimators = params.get('n_estimators', 100)
        max_depth = params.get('max_depth', 5)
        learning_rate = params.get('learning_rate', 0.1)

        result = {
            'status': 'error',
            'model': {},
            'factors': {},
            'prediction': {},
            'regime': {},
            'signal': {},
            'history': [],
        }

        if len(df) < train_window + 60:
            result['status'] = 'insufficient_data'
            result['model']['message'] = f'需要至少 {train_window + 60} 根K线，当前 {len(df)} 根'
            return result

        # 1. 计算因子
        fdf = AIMultiFactorStrategy.compute_factors(df)
        factor_cols = AIMultiFactorStrategy.get_factor_columns(fdf)

        # 2. 准备训练数据
        train_df = fdf.iloc[-(train_window + 1):-1].copy()
        train_df = train_df[factor_cols + ['close']].dropna()
        if len(train_df) < 100:
            result['status'] = 'insufficient_clean_data'
            return result

        train_df['target'] = (train_df['close'].shift(-1) / train_df['close'] - 1 > target_return).astype(int)
        train_df = train_df.dropna(subset=['target'])

        X_train = train_df[factor_cols].values
        y_train = train_df['target'].values

        if len(np.unique(y_train)) < 2:
            result['status'] = 'single_class'
            return result

        # 3. 训练模型
        model = XGBClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=learning_rate,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=1.0,
            use_label_encoder=False,
            eval_metric='logloss',
            random_state=42,
            n_jobs=1,
        )
        model.fit(X_train, y_train)

        # 4. 特征重要性
        importances = model.feature_importances_
        factor_importance = sorted(
            zip(factor_cols, importances.tolist()),
            key=lambda x: x[1],
            reverse=True
        )
        # 归一化到 0-100
        max_imp = max(imp for _, imp in factor_importance) if factor_importance else 1
        factor_importance = [(name, round(imp / max_imp * 100, 1)) for name, imp in factor_importance]

        # 5. 预测最新一根
        latest = fdf.iloc[[-1]][factor_cols].fillna(0)
        prob = model.predict_proba(latest.values)[0]
        buy_prob = float(prob[1]) if len(prob) > 1 else float(prob[0])

        current_price = float(df.iloc[-1]['close'])

        # 信号判断
        if buy_prob >= buy_threshold:
            signal = 'buy'
            signal_text = '建议买入'
            signal_confidence = buy_prob
        elif buy_prob <= sell_threshold:
            signal = 'sell'
            signal_text = '建议卖出'
            signal_confidence = 1 - buy_prob
        else:
            signal = 'hold'
            signal_text = '观望'
            signal_confidence = max(buy_prob, 1 - buy_prob)

        # 6. 市场状态判断
        ret_20 = df['close'].pct_change(20).iloc[-1]
        vol_20 = df['close'].pct_change().rolling(20).std().iloc[-1]
        sma_50 = df['close'].rolling(50).mean().iloc[-1]

        if ret_20 > 0.05 and current_price > sma_50:
            regime = 'bull'
            regime_text = '牛市'
            regime_color = 'green'
        elif ret_20 < -0.05 and current_price < sma_50:
            regime = 'bear'
            regime_text = '熊市'
            regime_color = 'red'
        else:
            regime = 'sideways'
            regime_text = '震荡'
            regime_color = 'yellow'

        # 7. 模型性能指标（在训练集上的表现）
        train_pred = model.predict(X_train)
        train_acc = float(np.mean(train_pred == y_train))

        # Walk-forward 验证（最后 20% 做验证）
        split = int(len(X_train) * 0.8)
        val_pred = model.predict(X_train[split:])
        val_acc = float(np.mean(val_pred == y_train[split:]))

        # 8. 最近 N 根 K 线的预测概率历史
        history = []
        n_hist = min(30, len(fdf) - train_window - 1)
        for i in range(-n_hist, 0):
            try:
                row = fdf.iloc[i - 1][factor_cols].fillna(0).values.reshape(1, -1)
                if not np.isnan(row).any():
                    p = model.predict_proba(row)[0]
                    bp = float(p[1]) if len(p) > 1 else float(p[0])
                    history.append({
                        'time': str(fdf.index[i - 1]),
                        'price': round(float(fdf.iloc[i - 1]['close']), 2),
                        'buy_prob': round(bp * 100, 1),
                    })
            except Exception:
                continue

        result = {
            'status': 'ok',
            'model': {
                'type': 'XGBClassifier',
                'n_estimators': n_estimators,
                'max_depth': max_depth,
                'train_samples': len(X_train),
                'train_accuracy': round(train_acc * 100, 1),
                'val_accuracy': round(val_acc * 100, 1),
                'positive_ratio': round(float(np.mean(y_train)) * 100, 1),
                'n_features': len(factor_cols),
            },
            'factors': {
                'top_15': [{'name': n, 'importance': v} for n, v in factor_importance[:15]],
                'categories': AIMultiFactorStrategy._factor_category_importance(factor_importance),
            },
            'prediction': {
                'buy_probability': round(buy_prob * 100, 1),
                'sell_probability': round((1 - buy_prob) * 100, 1),
                'current_price': round(current_price, 2),
                'threshold_buy': buy_threshold * 100,
                'threshold_sell': sell_threshold * 100,
            },
            'regime': {
                'state': regime,
                'text': regime_text,
                'color': regime_color,
                'volatility': round(float(vol_20 * 100), 2),
                'trend_20d': round(float(ret_20 * 100), 2),
            },
            'signal': {
                'action': signal,
                'text': signal_text,
                'confidence': round(signal_confidence * 100, 1),
                'price': round(current_price, 2),
            },
            'history': history,
        }

        return result

    @staticmethod
    def _factor_category_importance(factor_importance: list) -> list:
        """按因子类别汇总重要性"""
        categories = {
            '动量': [], '波动率': [], '技术指标': [], '趋势': [],
            '量价': [], '形态': [], '均值回归': [], '时间': [],
        }
        cat_prefix = {
            'mom_': '动量',
            'vol_ratio_': '波动率', 'vol_5': '波动率', 'vol_10': '波动率', 'vol_20': '波动率', 'vol_60': '波动率',
            'rsi_': '技术指标', 'macd_': '技术指标', 'bb_': '技术指标', 'atr': '技术指标',
            'sma_dist': '趋势', 'ema_dist': '趋势',
            'vol_ratio': '量价', 'price_vol': '量价', 'obv_': '量价',
            'body_': '形态', 'upper_': '形态', 'lower_': '形态',
            'zscore_': '均值回归',
            'hour_': '时间', 'dow_': '时间',
        }
        for name, imp in factor_importance:
            assigned = False
            # 按前缀长度降序匹配，避免短前缀误匹配
            sorted_prefixes = sorted(cat_prefix.keys(), key=len, reverse=True)
            for prefix in sorted_prefixes:
                if name.startswith(prefix):
                    categories[cat_prefix[prefix]].append(imp)
                    assigned = True
                    break
            if not assigned:
                # fallback: 检查包含关系
                for prefix in sorted_prefixes:
                    if prefix in name:
                        categories[cat_prefix[prefix]].append(imp)
                        break

        return [
            {'name': k, 'avg_importance': round(sum(v) / len(v), 1) if v else 0, 'count': len(v)}
            for k, v in sorted(categories.items(), key=lambda x: sum(x[1]) / max(len(x[1]), 1), reverse=True)
            if v
        ]


# ================================================================
# 交易所客户端 — 含本地缓存
# ================================================================

_CACHE_DIR = DATA_DIR / 'cache'
_CACHE_DIR.mkdir(exist_ok=True)
_CACHE_TTL = 300  # 5分钟缓存


class ExchangeClient:
    def __init__(self, exchange_id='binance', api_key='', api_secret='', passphrase=''):
        self.exchange_id = exchange_id
        cls = getattr(ccxt, exchange_id)
        params = {'enableRateLimit': True}
        if api_key:
            params['apiKey'] = api_key
            params['secret'] = api_secret
            if passphrase:
                params['password'] = passphrase
        self.exchange = cls(params)

    def _cache_key(self, symbol, timeframe, limit=None, since=None, end=None):
        raw = f"{self.exchange_id}:{symbol}:{timeframe}:{limit}:{since}:{end}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _read_cache(self, key):
        path = _CACHE_DIR / f"{key}.pkl"
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if age < _CACHE_TTL:
                try:
                    return pickle.loads(path.read_bytes())
                except Exception:
                    pass
        return None

    def _write_cache(self, key, data):
        try:
            (_CACHE_DIR / f"{key}.pkl").write_bytes(pickle.dumps(data))
        except Exception:
            pass

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=500):
        key = self._cache_key(symbol, timeframe, limit=limit)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        data = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df = df.astype(float)
        self._write_cache(key, df)
        return df

    def fetch_ohlcv_range(self, symbol, timeframe, start, end):
        # 尝试从本地CSV缓存读取
        cache_file = _CACHE_DIR / f"{symbol.replace('/','_')}_{timeframe}_{start}_{end}.csv"
        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < 3600:  # 范围数据缓存1小时
                return pd.read_csv(cache_file, index_col=0, parse_dates=True)

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
        df = df.astype(float)
        # 写入缓存
        try:
            df.to_csv(cache_file)
        except Exception:
            pass
        return df

    def create_market_order(self, symbol, side, amount):
        return self.exchange.create_order(symbol, 'market', side, amount)

    def fetch_balance(self):
        return self.exchange.fetch_balance()

    def fetch_positions(self, symbols=None):
        try:
            return self.exchange.fetch_positions(symbols)
        except Exception:
            return []

    @staticmethod
    def _tf_ms(tf):
        units = {'s':1000,'m':60000,'h':3600000,'d':86400000,'w':604800000}
        return int(tf[:-1]) * units[tf[-1]]


# ================================================================
# 回测引擎 — 向量化优化版
# ================================================================

class BacktestEngine:
    """
    向量化回测引擎
    """

    @staticmethod
    def run(df, strategy_type, params, capital=10000, commission=0.0004,
            slippage=0.0005, leverage=1, position_pct=10, stop_loss_pct=3,
            take_profit_pct=6, trailing_stop=True, trailing_pct=2):
        df = Indicators.add_all(df)

        buy_signals, sell_signals = BacktestEngine._vectorized_signals(
            df, strategy_type, params
        )

        close = df['close'].values
        n = len(close)

        cash = capital
        trades = []
        equity_curve = np.zeros(n)
        max_equity = capital
        max_dd = 0.0

        has_pos = False
        pos_entry = 0.0
        pos_size = 0.0
        pos_side = 0
        pos_highest = 0.0
        pos_lowest = 0.0

        for i in range(n):
            price = close[i]

            # 持仓管理
            if has_pos:
                if pos_side == 1:
                    pnl_pct = (price / pos_entry - 1) * 100
                    pos_highest = max(pos_highest, price)
                else:
                    pnl_pct = (pos_entry / price - 1) * 100
                    pos_lowest = min(pos_lowest, price)

                should_exit = False
                exit_reason = ''

                if pnl_pct <= -stop_loss_pct:
                    should_exit = True
                    exit_reason = 'stop_loss'
                elif pnl_pct >= take_profit_pct:
                    should_exit = True
                    exit_reason = 'take_profit'
                elif trailing_stop and pnl_pct > 0:
                    if pos_side == 1:
                        pullback = (pos_highest - price) / pos_highest * 100
                        if pullback >= trailing_pct:
                            should_exit = True
                            exit_reason = 'trailing_stop'
                    else:
                        pullback = (price - pos_lowest) / pos_lowest * 100
                        if pullback >= trailing_pct:
                            should_exit = True
                            exit_reason = 'trailing_stop'

                if should_exit:
                    slippage_adj = (1 - slippage) if pos_side == 1 else (1 + slippage)
                    exit_price = price * slippage_adj
                    pnl = (exit_price - pos_entry) * pos_size * pos_side
                    fee = abs(pnl) * commission if pnl > 0 else pos_size * exit_price * commission
                    cash += pnl - fee
                    trades.append({
                        'side': 'long' if pos_side == 1 else 'short',
                        'entry': pos_entry, 'exit_price': exit_price,
                        'pnl': pnl - fee, 'pnl_pct': pnl_pct, 'reason': exit_reason,
                    })
                    has_pos = False
                    pos_side = 0

            # 开仓信号
            if not has_pos:
                if buy_signals[i]:
                    entry = price * (1 + slippage)
                    size = (cash * position_pct / 100 * leverage) / entry
                    fee = size * entry * commission
                    cash -= fee
                    has_pos = True
                    pos_entry = entry
                    pos_size = size
                    pos_side = 1
                    pos_highest = entry
                elif sell_signals[i]:
                    entry = price * (1 - slippage)
                    size = (cash * position_pct / 100 * leverage) / entry
                    fee = size * entry * commission
                    cash -= fee
                    has_pos = True
                    pos_entry = entry
                    pos_size = size
                    pos_side = -1
                    pos_lowest = entry

            # 末尾平仓
            if has_pos and i == n - 1:
                slippage_adj = (1 - slippage) if pos_side == 1 else (1 + slippage)
                exit_price = price * slippage_adj
                pnl = (exit_price - pos_entry) * pos_size * pos_side
                fee = abs(pnl) * commission if pnl > 0 else pos_size * exit_price * commission
                cash += pnl - fee
                trades.append({
                    'side': 'long' if pos_side == 1 else 'short',
                    'entry': pos_entry, 'exit_price': exit_price,
                    'pnl': pnl - fee,
                    'pnl_pct': (pnl / (pos_entry * pos_size)) * 100,
                    'reason': 'end',
                })
                has_pos = False

            eq = cash
            if has_pos:
                eq += (price - pos_entry) * pos_size * pos_side
            equity_curve[i] = eq
            max_equity = max(max_equity, eq)
            dd = (max_equity - eq) / max_equity * 100 if max_equity > 0 else 0
            max_dd = max(max_dd, dd)

        wins = [t for t in trades if t.get('pnl', 0) > 0]
        losses = [t for t in trades if t.get('pnl', 0) <= 0]
        total_profit = sum(t['pnl'] for t in wins) if wins else 0
        total_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0

        returns = pd.Series(equity_curve).pct_change().dropna()
        sharpe = float(returns.mean() / returns.std() * np.sqrt(365*24)) if len(returns) > 1 and returns.std() > 0 else 0

        return {
            'initial_capital': capital,
            'final_capital': round(cash, 2),
            'total_return_pct': round((cash / capital - 1) * 100, 2),
            'total_trades': len(trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'avg_win_pct': round(float(np.mean([t['pnl_pct'] for t in wins])), 2) if wins else 0,
            'avg_loss_pct': round(float(np.mean([t['pnl_pct'] for t in losses])), 2) if losses else 0,
            'profit_factor': round(total_profit / total_loss, 2) if total_loss > 0 else 0,
            'max_drawdown_pct': round(max_dd, 2),
            'sharpe_ratio': round(sharpe, 2),
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
            'equity_curve': [round(float(e), 2) for e in equity_curve],
        }

    @staticmethod
    def _vectorized_signals(df, strategy_type, params):
        """向量化信号预计算 — 返回 (buy_mask, sell_mask) numpy 数组"""
        n = len(df)
        buy = np.zeros(n, dtype=bool)
        sell = np.zeros(n, dtype=bool)

        if strategy_type == 'macd_cross':
            macd = df['macd'].values
            sig = df['macd_signal'].values
            buy[1:] = (macd[:-1] <= sig[:-1]) & (macd[1:] > sig[1:])
            sell[1:] = (macd[:-1] >= sig[:-1]) & (macd[1:] < sig[1:])

        elif strategy_type == 'rsi_reversal':
            rsi = df['rsi'].values
            oversold = params.get('oversold', 30)
            overbought = params.get('overbought', 70)
            buy[1:] = (rsi[:-1] < oversold) & (rsi[1:] >= oversold)
            sell[1:] = (rsi[:-1] > overbought) & (rsi[1:] <= overbought)

        elif strategy_type == 'bollinger_breakout':
            close = df['close'].values
            upper = df['bb_upper'].values
            lower = df['bb_lower'].values
            buy[1:] = (close[:-1] <= upper[:-1]) & (close[1:] > upper[1:])
            sell[1:] = (close[:-1] >= lower[:-1]) & (close[1:] < lower[1:])

        elif strategy_type == 'dual_ma':
            fast_p = params.get('fast_period', 10)
            slow_p = params.get('slow_period', 30)
            ma_type = params.get('ma_type', 'ema')
            if ma_type == 'ema':
                fast_ma = df['close'].ewm(span=fast_p, adjust=False).mean().values
                slow_ma = df['close'].ewm(span=slow_p, adjust=False).mean().values
            else:
                fast_ma = df['close'].rolling(fast_p).mean().values
                slow_ma = df['close'].rolling(slow_p).mean().values
            buy[1:] = (fast_ma[:-1] <= slow_ma[:-1]) & (fast_ma[1:] > slow_ma[1:])
            sell[1:] = (fast_ma[:-1] >= slow_ma[:-1]) & (fast_ma[1:] < slow_ma[1:])

        elif strategy_type == 'ai_multi_factor':
            # AI 多因子使用 walk-forward 向量化信号
            return AIMultiFactorStrategy.vectorized_signals(df, params)

        elif strategy_type in ('lstm', 'transformer'):
            # 深度学习 walk-forward 向量化信号
            try:
                from engine.models import model_manager
                return model_manager.walk_forward_signals(
                    df, strategy_type,
                    train_window=params.get('train_window', 500),
                    retrain_interval=params.get('retrain_interval', 50),
                    seq_len=params.get('seq_len', 60),
                    epochs=params.get('epochs', 30),
                    target_return=params.get('target_return', 0.002),
                )
            except Exception as e:
                logger.error(f"DL walk-forward error: {e}")
                return buy, sell

        buy[:50] = False
        sell[:50] = False

        return buy, sell


# ================================================================
# 实盘执行器
# ================================================================

# ================================================================
# 实盘执行器
# ================================================================

class LiveTrader:
    """实盘交易执行器 -- 后台线程运行
    Phase 4: 集成风控模块 (熔断/凯利/ATR追踪止损/波动率检测)
    """

    def __init__(self, risk_mgr=None):
        self._strategies: Dict[str, Dict] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # 风控管理器（可注入，不强制依赖）
        self._risk = risk_mgr

    def _get_risk(self):
        """延迟加载风控管理器"""
        if self._risk is None:
            try:
                from engine.risk import risk_manager
                self._risk = risk_manager
            except ImportError:
                pass
        return self._risk

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
                'highest_price': 0.0,
                'lowest_price': 0.0,
                'peak_equity': config.capital,
            }

            t = threading.Thread(target=self._run_loop, args=(sid,), daemon=True)
            self._threads[sid] = t
            t.start()

            logger.info(f"Strategy {config.name} started (risk: {self._get_risk() is not None})")
            return True

    def stop(self, sid: str):
        with self._lock:
            if sid in self._stop_events:
                self._stop_events[sid].set()
                del self._stop_events[sid]
            if sid in self._threads:
                del self._threads[sid]
            if sid in self._strategies:
                self._strategies[sid]['config'].status = 'stopped'

    def get_status(self, sid: str) -> Optional[Dict]:
        return self._strategies.get(sid)

    def get_all_status(self) -> Dict:
        return self._strategies

    def _close_position(self, state: dict, config: StrategyConfig,
                        client, current_price: float, reason: str):
        """统一平仓逻辑: 更新权益 + 报告风控 + 下单"""
        sym = config.symbol
        if sym not in state['positions']:
            return

        pos = state['positions'].pop(sym)
        side_multiplier = 1 if pos['side'] == 'long' else -1
        pnl = (current_price - pos.get('entry_price', current_price)) * pos.get('size', 0) * side_multiplier
        entry_val = pos.get('entry_price', 1) * pos.get('size', 1)
        pnl_pct = (pnl / entry_val * 100) if entry_val > 0 else 0

        state['equity'] += pnl
        trade_record = {
            'type': reason,
            'side': pos['side'],
            'entry_price': round(pos.get('entry_price', 0), 2),
            'exit_price': round(current_price, 2),
            'size': round(pos.get('size', 0), 6),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
            'time': datetime.now().isoformat(),
        }
        state['trades'].append(trade_record)

        # 实盘下单
        if not config.paper and config.api_key:
            try:
                close_side = 'sell' if pos['side'] == 'long' else 'buy'
                client.create_market_order(config.symbol, close_side, pos['size'])
                logger.info(f"Position closed: {sym} {reason} pnl={pnl:.2f}")
            except Exception as e:
                state['errors'].append(f"close order failed: {e}")
                logger.error(f"Close order failed: {e}")

        # 报告风控模块
        risk = self._get_risk()
        if risk:
            risk.on_trade_result(config.id, pnl_pct)

        logger.info(f"Strategy {config.id} closed {pos['side']} @ {current_price:.2f} "
                    f"pnl={pnl:.2f} ({pnl_pct:.2f}%) reason={reason}")

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
                min_bars = 600 if config.type == 'ai_multi_factor' else 200
                df = client.fetch_ohlcv(config.symbol, config.timeframe, limit=min_bars)
                if df.empty:
                    stop_event.wait(interval)
                    continue

                current_price = float(df.iloc[-1]['close'])
                risk = self._get_risk()

                # ============================================================
                # Phase 4-A: 市场环境风险检查 (波动率/闪崩检测)
                # ============================================================
                if risk:
                    risk.update_equity(state['equity'])
                    state['peak_equity'] = max(state['peak_equity'], state['equity'])

                    market_risk = risk.check_market_conditions(
                        df['close'].values, config.symbol
                    )
                    if market_risk.get('action') == 'halt_all':
                        logger.warning(f"Market HALT: {config.symbol} level={market_risk['risk_level']}")
                        for sym in list(state['positions'].keys()):
                            self._close_position(state, config, client, current_price, 'market_halt')
                        stop_event.wait(interval)
                        continue

                # ============================================================
                # Phase 4-B: 熔断检查
                # ============================================================
                if risk:
                    pos_check = risk.check_position(
                        strategy_id=config.id,
                        symbol=config.symbol,
                        entry_price=0,
                        current_price=current_price,
                        highest_price=state.get('highest_price', current_price),
                        lowest_price=state.get('lowest_price', current_price),
                        side='long',
                        equity=state['equity'],
                        capital=config.capital,
                    )
                    action = pos_check.get('action', 'hold')
                    if action == 'halt':
                        for sym in list(state['positions'].keys()):
                            self._close_position(state, config, client, current_price, 'risk_halt')
                        stop_event.wait(min(interval, 60))
                        continue
                    if action == 'close':
                        for sym in list(state['positions'].keys()):
                            self._close_position(state, config, client, current_price, 'risk_close')
                        continue

                # ============================================================
                # 生成信号
                # ============================================================
                signals = StrategyEngine.generate_signals(df, config.type, config.params)

                for sig in signals[-1:]:
                    if sig['type'] == 'buy':
                        if config.symbol in state['positions'] and state['positions'][config.symbol]['side'] == 'short':
                            self._close_position(state, config, client, current_price, 'signal_close_short')

                        if not state['positions']:
                            # Phase 4-C: 凯利动态仓位
                            position_pct = config.position_size_pct
                            if risk:
                                kelly = risk.kelly.calculate(config.id)
                                position_pct = kelly.get('position_pct', position_pct)
                                state['kelly_info'] = kelly

                            amount_usdt = config.capital * position_pct / 100 * config.leverage
                            if not config.paper and config.api_key:
                                try:
                                    size = amount_usdt / current_price
                                    order = client.create_market_order(config.symbol, 'buy', size)
                                    logger.info(f"Order placed: {order}")
                                except Exception as e:
                                    state['errors'].append(str(e))
                                    if len(state['errors']) > 50:
                                        state['errors'] = state['errors'][-50:]
                                    logger.error(f"Order failed: {e}")
                                    continue
                            state['positions'][config.symbol] = {
                                'side': 'long',
                                'size': amount_usdt / current_price,
                                'entry_price': current_price,
                                'current_price': current_price,
                                'opened_at': datetime.now().isoformat(),
                            }
                            state['highest_price'] = current_price
                            state['lowest_price'] = current_price
                            logger.info(f"Strategy {config.id} LONG size={amount_usdt:.2f} "
                                       f"@ {current_price:.2f} pct={position_pct:.1f}%")
                        state['last_signal'] = 'buy'

                    elif sig['type'] == 'sell':
                        if config.symbol in state['positions'] and state['positions'][config.symbol]['side'] == 'long':
                            self._close_position(state, config, client, current_price, 'signal_close_long')
                        state['last_signal'] = 'sell'

                # ============================================================
                # Phase 4-D: 高级持仓风控
                # ============================================================
                for sym in list(state['positions'].keys()):
                    pos = state['positions'][sym]
                    pos['current_price'] = current_price
                    state['highest_price'] = max(state.get('highest_price', current_price), current_price)
                    state['lowest_price'] = min(state.get('lowest_price', current_price), current_price)

                    pnl_pct = (current_price / pos['entry_price'] - 1) * 100
                    if pos['side'] == 'short':
                        pnl_pct = -pnl_pct
                    pos['pnl_pct'] = round(pnl_pct, 2)

                    # ATR
                    atr_val = 0
                    if 'atr' in df.columns:
                        atr_val = float(df['atr'].iloc[-1]) if pd.notna(df['atr'].iloc[-1]) else 0

                    # 1) ATR 自适应追踪止损
                    if risk and atr_val > 0:
                        stop_result = risk.trailing_stop.check(
                            entry_price=pos['entry_price'],
                            current_price=current_price,
                            highest_price=state['highest_price'],
                            lowest_price=state['lowest_price'],
                            side=pos['side'],
                            atr=atr_val,
                        )
                        if stop_result.get('triggered'):
                            self._close_position(state, config, client, current_price, 'atr_trailing_stop')
                            continue

                    # 2) 固定止损 (兜底)
                    if pnl_pct <= -config.stop_loss_pct:
                        self._close_position(state, config, client, current_price, 'stop_loss')
                        continue

                    # 3) 固定止盈
                    if pnl_pct >= config.take_profit_pct:
                        self._close_position(state, config, client, current_price, 'take_profit')
                        continue

                    # 4) 全局回撤检查
                    if risk:
                        peak = state['peak_equity']
                        if peak > 0:
                            dd = (peak - state['equity']) / peak * 100
                            if dd >= config.max_drawdown_pct:
                                logger.warning(f"Max drawdown {dd:.1f}%, closing all")
                                self._close_position(state, config, client, current_price, 'max_drawdown')
                                from engine.risk import RiskEventType
                                risk._trigger_circuit_breaker(
                                    f"Strategy {config.id} drawdown {dd:.1f}%",
                                    RiskEventType.MAX_DRAWDOWN,
                                )
                                continue

                state['last_update'] = datetime.now().isoformat()

            except Exception as e:
                logger.error(f"Strategy {sid} error: {e}")
                state['errors'].append(f"{datetime.now():%H:%M:%S} {e}")
                if len(state['errors']) > 50:
                    state['errors'] = state['errors'][-50:]

            stop_event.wait(interval)

        logger.info(f"Strategy {sid} stopped")
