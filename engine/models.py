"""
MyTradingPlatform — 深度学习预测模型
LSTM + Transformer 时序预测

Phase 5: AI 增强层
- 双层 LSTM (带 Dropout + LayerNorm)
- Transformer Encoder (多头注意力 + 位置编码)
- Walk-Forward 滚动训练
- 模型持久化与版本管理
- 在线学习 / 增量更新
"""

import os
import json
import time
import logging
import hashlib
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
MODEL_DIR = BASE_DIR / 'data' / 'models'
MODEL_DIR.mkdir(parents=True, exist_ok=True)

DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# ================================================================
# 数据集
# ================================================================

class TimeSeriesDataset(Dataset):
    """滑动窗口时序数据集"""

    def __init__(self, X: np.ndarray, y: np.ndarray):
        self.X = torch.FloatTensor(X)
        self.y = torch.FloatTensor(y)

    def __len__(self):
        return len(self.X)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]


# ================================================================
# LSTM 模型
# ================================================================

class LSTMPredictor(nn.Module):
    """
    双层 LSTM 价格方向预测器

    架构:
    Input -> LSTM(layer1) -> Dropout -> LSTM(layer2) -> LayerNorm
          -> FC(hidden) -> ReLU -> Dropout -> FC(out) -> Sigmoid
    """

    def __init__(self, input_size: int, hidden_size: int = 128,
                 num_layers: int = 2, dropout: float = 0.3):
        super().__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers

        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0,
        )
        self.layer_norm = nn.LayerNorm(hidden_size)
        self.fc1 = nn.Linear(hidden_size, hidden_size // 2)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.fc2 = nn.Linear(hidden_size // 2, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # x: (batch, seq_len, features)
        lstm_out, _ = self.lstm(x)
        # 取最后一个时间步
        out = lstm_out[:, -1, :]
        out = self.layer_norm(out)
        out = self.fc1(out)
        out = self.relu(out)
        out = self.dropout(out)
        out = self.fc2(out)
        return self.sigmoid(out).squeeze(-1)


# ================================================================
# Transformer 模型
# ================================================================

class PositionalEncoding(nn.Module):
    """正弦位置编码"""

    def __init__(self, d_model: int, max_len: int = 1000):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe.unsqueeze(0))

    def forward(self, x):
        return x + self.pe[:, :x.size(1)]


class TransformerPredictor(nn.Module):
    """
    Transformer Encoder 价格方向预测器

    架构:
    Input -> Linear(embed) -> PosEncode -> TransformerEncoder
          -> GlobalAvgPool -> FC(hidden) -> ReLU -> Dropout -> FC(out) -> Sigmoid
    """

    def __init__(self, input_size: int, d_model: int = 64,
                 nhead: int = 4, num_layers: int = 3,
                 dim_feedforward: int = 256, dropout: float = 0.2):
        super().__init__()
        self.d_model = d_model

        # 输入投影
        self.input_proj = nn.Linear(input_size, d_model)
        self.pos_encoder = PositionalEncoding(d_model)

        # Transformer Encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=dim_feedforward,
            dropout=dropout,
            batch_first=True,
            activation='gelu',
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # 输出头
        self.fc1 = nn.Linear(d_model, d_model // 2)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.fc2 = nn.Linear(d_model // 2, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # x: (batch, seq_len, features)
        # 投影到 d_model 维度
        x = self.input_proj(x) * np.sqrt(self.d_model)
        x = self.pos_encoder(x)

        # Transformer 编码
        x = self.transformer(x)

        # 全局平均池化
        x = x.mean(dim=1)

        x = self.fc1(x)
        x = self.relu(x)
        x = self.dropout(x)
        x = self.fc2(x)
        return self.sigmoid(x).squeeze(-1)


# ================================================================
# 特征工程（复用 + 扩展）
# ================================================================

class FeatureEngineer:
    """特征工程：从 OHLCV 生成模型输入特征"""

    @staticmethod
    def compute_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[str]]:
        """
        计算全部特征
        返回: (DataFrame with features, feature column names)
        """
        from engine.core import Indicators

        f = df.copy()
        close = f['close']
        high = f['high']
        low = f['low']
        volume = f['volume']

        # === 收益率特征 ===
        for p in [1, 3, 5, 10, 20]:
            f[f'ret_{p}'] = close.pct_change(p)

        # === 波动率特征 ===
        ret = close.pct_change()
        for p in [5, 10, 20]:
            f[f'vol_{p}'] = ret.rolling(p).std()

        # === 技术指标 ===
        f['rsi_14'] = Indicators.rsi(f, 14)
        f['rsi_7'] = Indicators.rsi(f, 7)
        macd_line, sig_line, hist = Indicators.macd(f)
        f['macd_line'] = macd_line
        f['macd_signal'] = sig_line
        f['macd_hist'] = hist
        upper, mid, lower = Indicators.bollinger(f)
        f['bb_upper'] = upper
        f['bb_middle'] = mid
        f['bb_lower'] = lower
        f['bb_width'] = (upper - lower) / mid
        f['bb_position'] = (close - lower) / (upper - lower).replace(0, np.nan)
        f['atr'] = Indicators.atr(f)
        f['atr_ratio'] = f['atr'] / close

        # === 均线距离 ===
        for p in [10, 20, 50]:
            sma = close.rolling(p).mean()
            ema = close.ewm(span=p, adjust=False).mean()
            f[f'sma_dist_{p}'] = (close - sma) / sma
            f[f'ema_dist_{p}'] = (close - ema) / ema

        # === 量价特征 ===
        vol_sma = volume.rolling(20).mean()
        f['vol_ratio'] = volume / vol_sma.replace(0, np.nan)
        f['price_vol_corr'] = ret.rolling(20).corr(volume.pct_change())

        # === K线形态 ===
        f['body_ratio'] = (close - f['open']).abs() / (high - low).replace(0, np.nan)
        f['hl_range'] = (high - low) / close

        # === Z-score ===
        for p in [20, 60]:
            sma = close.rolling(p).mean()
            std = close.rolling(p).std()
            f[f'zscore_{p}'] = (close - sma) / std.replace(0, np.nan)

        # === 时间特征 ===
        if hasattr(f.index, 'hour'):
            f['hour_sin'] = np.sin(2 * np.pi * f.index.hour / 24)
            f['hour_cos'] = np.cos(2 * np.pi * f.index.hour / 24)
        if hasattr(f.index, 'dayofweek'):
            f['dow_sin'] = np.sin(2 * np.pi * f.index.dayofweek / 7)
            f['dow_cos'] = np.cos(2 * np.pi * f.index.dayofweek / 7)

        # 选择特征列（排除原始 OHLCV）
        exclude = {'open', 'high', 'low', 'close', 'volume', 'timestamp'}
        feature_cols = [c for c in f.columns if c not in exclude and not c.startswith('_')]

        return f, feature_cols

    @staticmethod
    def prepare_sequences(df: pd.DataFrame, feature_cols: List[str],
                          seq_len: int = 60, target_return: float = 0.002,
                          scaler: StandardScaler = None) -> Tuple:
        """
        准备滑动窗口序列数据

        返回: (X, y, scaler, valid_indices)
        X: (N, seq_len, n_features)
        y: (N,)  1 = 下一根收益 > target_return, 0 = 否
        """
        # 提取特征并填充
        feat_df = df[feature_cols].copy()
        feat_df = feat_df.replace([np.inf, -np.inf], np.nan).fillna(0)

        # 标签：下一根 K 线收益
        returns = df['close'].pct_change().shift(-1)
        y_all = (returns > target_return).astype(float).values

        # 标准化
        if scaler is None:
            scaler = StandardScaler()
            scaled = scaler.fit_transform(feat_df.values)
        else:
            scaled = scaler.transform(feat_df.values)

        # 滑动窗口
        X, y, indices = [], [], []
        for i in range(seq_len, len(scaled) - 1):
            if not np.isnan(y_all[i]):
                X.append(scaled[i - seq_len:i])
                y.append(y_all[i])
                indices.append(i)

        return np.array(X), np.array(y), scaler, indices


# ================================================================
# 模型管理器
# ================================================================

@dataclass
class ModelMeta:
    model_type: str = ""       # lstm / transformer
    symbol: str = ""
    timeframe: str = ""
    seq_len: int = 60
    n_features: int = 0
    train_samples: int = 0
    val_accuracy: float = 0.0
    val_loss: float = 0.0
    trained_at: str = ""
    feature_cols: List[str] = field(default_factory=list)
    hyperparams: Dict = field(default_factory=dict)


class ModelManager:
    """
    深度学习模型管理器
    训练、预测、持久化、Walk-Forward 验证
    """

    def __init__(self):
        self._models: Dict[str, nn.Module] = {}
        self._scalers: Dict[str, StandardScaler] = {}
        self._meta: Dict[str, ModelMeta] = {}

    def _model_key(self, model_type: str, symbol: str, timeframe: str) -> str:
        return f"{model_type}:{symbol}:{timeframe}"

    # ---- 训练 ----

    def train(self, df: pd.DataFrame, model_type: str = 'lstm',
              symbol: str = 'BTC/USDT', timeframe: str = '1h',
              seq_len: int = 60, epochs: int = 50, batch_size: int = 64,
              lr: float = 0.001, target_return: float = 0.002,
              val_ratio: float = 0.2, **model_kwargs) -> Dict:
        """
        训练模型

        返回: {status, train_loss, val_loss, val_accuracy, train_samples, ...}
        """
        key = self._model_key(model_type, symbol, timeframe)

        # 1. 特征工程
        fdf, feature_cols = FeatureEngineer.compute_features(df)
        n_features = len(feature_cols)

        logger.info(f"Training {model_type} for {symbol} {timeframe}: "
                    f"{len(df)} bars, {n_features} features, seq_len={seq_len}")

        # 2. 准备数据
        X, y, scaler, indices = FeatureEngineer.prepare_sequences(
            fdf, feature_cols, seq_len, target_return
        )

        if len(X) < 100:
            return {'status': 'insufficient_data', 'samples': len(X), 'min_required': 100}

        # 3. 训练/验证分割
        split = int(len(X) * (1 - val_ratio))
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y[:split], y[split:]

        train_ds = TimeSeriesDataset(X_train, y_train)
        val_ds = TimeSeriesDataset(X_val, y_val)
        train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
        val_loader = DataLoader(val_ds, batch_size=batch_size)

        # 4. 创建模型
        if model_type == 'lstm':
            model = LSTMPredictor(
                input_size=n_features,
                hidden_size=model_kwargs.get('hidden_size', 128),
                num_layers=model_kwargs.get('num_layers', 2),
                dropout=model_kwargs.get('dropout', 0.3),
            )
        elif model_type == 'transformer':
            model = TransformerPredictor(
                input_size=n_features,
                d_model=model_kwargs.get('d_model', 64),
                nhead=model_kwargs.get('nhead', 4),
                num_layers=model_kwargs.get('num_layers', 3),
                dim_feedforward=model_kwargs.get('dim_feedforward', 256),
                dropout=model_kwargs.get('dropout', 0.2),
            )
        else:
            return {'status': 'error', 'message': f'Unknown model type: {model_type}'}

        model = model.to(DEVICE)
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode='min', factor=0.5, patience=5
        )
        criterion = nn.BCELoss()

        # 5. 训练循环
        best_val_loss = float('inf')
        best_state = None
        patience_counter = 0
        train_losses = []
        val_losses = []

        for epoch in range(epochs):
            # Train
            model.train()
            epoch_loss = 0
            for xb, yb in train_loader:
                xb, yb = xb.to(DEVICE), yb.to(DEVICE)
                optimizer.zero_grad()
                pred = model(xb)
                loss = criterion(pred, yb)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                epoch_loss += loss.item() * len(xb)
            train_loss = epoch_loss / len(train_ds)
            train_losses.append(train_loss)

            # Validate
            model.eval()
            val_loss = 0
            val_preds, val_targets = [], []
            with torch.no_grad():
                for xb, yb in val_loader:
                    xb, yb = xb.to(DEVICE), yb.to(DEVICE)
                    pred = model(xb)
                    loss = criterion(pred, yb)
                    val_loss += loss.item() * len(xb)
                    val_preds.extend(pred.cpu().numpy())
                    val_targets.extend(yb.cpu().numpy())
            val_loss /= len(val_ds)
            val_losses.append(val_loss)

            val_preds = np.array(val_preds)
            val_targets = np.array(val_targets)
            val_acc = np.mean((val_preds > 0.5) == val_targets)

            scheduler.step(val_loss)

            # Early stopping
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
                patience_counter = 0
            else:
                patience_counter += 1

            if (epoch + 1) % 10 == 0:
                logger.info(f"  Epoch {epoch+1}/{epochs} train_loss={train_loss:.4f} "
                           f"val_loss={val_loss:.4f} val_acc={val_acc:.4f}")

            if patience_counter >= 10:
                logger.info(f"  Early stopping at epoch {epoch+1}")
                break

        # 恢复最佳模型
        if best_state:
            model.load_state_dict(best_state)

        model = model.to(DEVICE)
        model.eval()

        # 6. 保存
        self._models[key] = model
        self._scalers[key] = scaler
        self._meta[key] = ModelMeta(
            model_type=model_type,
            symbol=symbol,
            timeframe=timeframe,
            seq_len=seq_len,
            n_features=n_features,
            train_samples=len(X_train),
            val_accuracy=round(float(val_acc), 4),
            val_loss=round(float(best_val_loss), 4),
            trained_at=datetime.now().isoformat(),
            feature_cols=feature_cols,
            hyperparams=model_kwargs,
        )

        # 持久化到磁盘
        self._save_model(key, model, scaler, self._meta[key])

        logger.info(f"Training complete: {model_type} val_acc={val_acc:.4f} "
                    f"val_loss={best_val_loss:.4f}")

        return {
            'status': 'ok',
            'model_type': model_type,
            'symbol': symbol,
            'timeframe': timeframe,
            'train_samples': len(X_train),
            'val_samples': len(X_val),
            'val_accuracy': round(float(val_acc) * 100, 2),
            'val_loss': round(float(best_val_loss), 4),
            'train_loss_history': [round(l, 4) for l in train_losses[-10:]],
            'val_loss_history': [round(l, 4) for l in val_losses[-10:]],
            'n_features': n_features,
            'seq_len': seq_len,
            'epochs_run': len(train_losses),
            'device': str(DEVICE),
        }

    # ---- 预测 ----

    def predict(self, df: pd.DataFrame, model_type: str = 'lstm',
                symbol: str = 'BTC/USDT', timeframe: str = '1h') -> Dict:
        """
        使用训练好的模型预测

        返回: {buy_probability, signal, confidence, model_info, ...}
        """
        key = self._model_key(model_type, symbol, timeframe)

        # 尝试加载
        if key not in self._models:
            self._load_model(key)

        if key not in self._models:
            return {'status': 'no_model', 'message': f'Model not trained: {key}'}

        model = self._models[key]
        scaler = self._scalers[key]
        meta = self._meta[key]

        # 特征工程
        fdf, feature_cols = FeatureEngineer.compute_features(df)

        # 确保特征列一致
        for col in meta.feature_cols:
            if col not in fdf.columns:
                fdf[col] = 0
        feat_df = fdf[meta.feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0)

        # 构建序列
        scaled = scaler.transform(feat_df.values)
        seq_len = meta.seq_len

        if len(scaled) < seq_len:
            return {'status': 'insufficient_data', 'have': len(scaled), 'need': seq_len}

        # 取最新 seq_len 根
        x = scaled[-seq_len:].reshape(1, seq_len, -1)
        x_tensor = torch.FloatTensor(x).to(DEVICE)

        model.eval()
        with torch.no_grad():
            prob = model(x_tensor).item()

        # 信号判断
        if prob >= 0.6:
            signal = 'buy'
            confidence = prob
        elif prob <= 0.4:
            signal = 'sell'
            confidence = 1 - prob
        else:
            signal = 'hold'
            confidence = max(prob, 1 - prob)

        current_price = float(df['close'].iloc[-1])

        return {
            'status': 'ok',
            'model_type': model_type,
            'symbol': symbol,
            'buy_probability': round(prob * 100, 1),
            'sell_probability': round((1 - prob) * 100, 1),
            'signal': signal,
            'confidence': round(confidence * 100, 1),
            'current_price': round(current_price, 2),
            'model_info': {
                'val_accuracy': round(meta.val_accuracy * 100, 1),
                'train_samples': meta.train_samples,
                'n_features': meta.n_features,
                'trained_at': meta.trained_at,
            },
        }

    def predict_both(self, df: pd.DataFrame,
                     symbol: str = 'BTC/USDT',
                     timeframe: str = '1h') -> Dict:
        """同时用 LSTM 和 Transformer 预测，取集成结果"""
        lstm_result = self.predict(df, 'lstm', symbol, timeframe)
        tf_result = self.predict(df, 'transformer', symbol, timeframe)

        results = {'lstm': lstm_result, 'transformer': tf_result}

        # 集成
        probs = []
        if lstm_result['status'] == 'ok':
            probs.append(lstm_result['buy_probability'] / 100)
        if tf_result['status'] == 'ok':
            probs.append(tf_result['buy_probability'] / 100)

        if not probs:
            return {'status': 'no_models', 'details': results}

        ensemble_prob = np.mean(probs)
        if ensemble_prob >= 0.6:
            signal = 'buy'
        elif ensemble_prob <= 0.4:
            signal = 'sell'
        else:
            signal = 'hold'

        return {
            'status': 'ok',
            'symbol': symbol,
            'ensemble': {
                'buy_probability': round(ensemble_prob * 100, 1),
                'signal': signal,
                'confidence': round(max(ensemble_prob, 1 - ensemble_prob) * 100, 1),
                'agreement': abs(probs[0] - probs[1]) < 0.1 if len(probs) == 2 else True,
            },
            'details': results,
        }

    # ---- Walk-Forward 回测信号 ----

    def walk_forward_signals(self, df: pd.DataFrame, model_type: str = 'lstm',
                              symbol: str = 'BTC/USDT', timeframe: str = '1h',
                              train_window: int = 500, retrain_interval: int = 50,
                              seq_len: int = 60, epochs: int = 30,
                              target_return: float = 0.002,
                              **kwargs) -> Tuple[np.ndarray, np.ndarray]:
        """
        Walk-Forward 向量化信号生成（回测用）
        返回: (buy_mask, sell_mask)
        """
        fdf, feature_cols = FeatureEngineer.compute_features(df)
        n = len(df)
        buy = np.zeros(n, dtype=bool)
        sell = np.zeros(n, dtype=bool)

        # 标签
        returns = df['close'].pct_change().shift(-1)
        y_all = (returns > target_return).astype(float).values

        # 特征矩阵
        feat_data = fdf[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0).values

        # 需要足够的数据
        min_data = train_window + seq_len + 100
        if len(feat_data) < min_data:
            logger.warning(f"Insufficient data for walk-forward: {len(feat_data)} < {min_data}")
            return buy, sell

        model = None
        scaler = None
        last_train_end = -1

        for i in range(train_window + seq_len, n - 1):
            # 需要重新训练
            if model is None or (i - last_train_end) >= retrain_interval:
                train_start = max(0, i - train_window - seq_len)
                train_feat = feat_data[train_start:i]
                train_y = y_all[train_start:i]

                # 有效数据
                valid = ~np.isnan(train_y)
                if valid.sum() < 100:
                    continue

                # 标准化
                scaler = StandardScaler()
                scaled = scaler.fit_transform(train_feat)

                # 构建序列
                X_seq, y_seq = [], []
                for j in range(seq_len, len(scaled) - 1):
                    if not np.isnan(train_y[j]):
                        X_seq.append(scaled[j - seq_len:j])
                        y_seq.append(train_y[j])

                if len(X_seq) < 100 or len(np.unique(y_seq)) < 2:
                    continue

                X_seq = np.array(X_seq)
                y_seq = np.array(y_seq)

                # 快速训练
                if model_type == 'lstm':
                    model = LSTMPredictor(
                        input_size=len(feature_cols),
                        hidden_size=64,
                        num_layers=1,
                        dropout=0.2,
                    ).to(DEVICE)
                else:
                    model = TransformerPredictor(
                        input_size=len(feature_cols),
                        d_model=32,
                        nhead=4,
                        num_layers=2,
                        dropout=0.2,
                    ).to(DEVICE)

                optimizer = torch.optim.Adam(model.parameters(), lr=0.002)
                criterion = nn.BCELoss()
                dataset = TimeSeriesDataset(X_seq, y_seq)
                loader = DataLoader(dataset, batch_size=64, shuffle=True)

                model.train()
                for _ in range(epochs):
                    for xb, yb in loader:
                        xb, yb = xb.to(DEVICE), yb.to(DEVICE)
                        optimizer.zero_grad()
                        pred = model(xb)
                        loss = criterion(pred, yb)
                        loss.backward()
                        optimizer.step()

                model.eval()
                last_train_end = i

            if model is None or scaler is None:
                continue

            # 预测当前点
            window = feat_data[i - seq_len:i]
            scaled_window = scaler.transform(window).reshape(1, seq_len, -1)
            x_tensor = torch.FloatTensor(scaled_window).to(DEVICE)

            with torch.no_grad():
                prob = model(x_tensor).item()

            if prob >= 0.6:
                buy[i] = True
            elif prob <= 0.4:
                sell[i] = True

        # 清理前端
        buy[:train_window + seq_len] = False
        sell[:train_window + seq_len] = False

        return buy, sell

    # ---- 持久化 ----

    def _save_model(self, key: str, model: nn.Module,
                    scaler: StandardScaler, meta: ModelMeta):
        """保存模型到磁盘"""
        safe_key = key.replace('/', '_').replace(':', '_')
        model_path = MODEL_DIR / f"{safe_key}.pt"
        meta_path = MODEL_DIR / f"{safe_key}.json"
        scaler_path = MODEL_DIR / f"{safe_key}_scaler.npz"

        torch.save(model.state_dict(), model_path)

        import pickle
        with open(scaler_path, 'wb') as f:
            pickle.dump(scaler, f)

        meta_dict = {
            'model_type': meta.model_type,
            'symbol': meta.symbol,
            'timeframe': meta.timeframe,
            'seq_len': meta.seq_len,
            'n_features': meta.n_features,
            'train_samples': meta.train_samples,
            'val_accuracy': meta.val_accuracy,
            'val_loss': meta.val_loss,
            'trained_at': meta.trained_at,
            'feature_cols': meta.feature_cols,
            'hyperparams': meta.hyperparams,
        }
        meta_path.write_text(json.dumps(meta_dict, ensure_ascii=False, indent=2))

    def _load_model(self, key: str) -> bool:
        """从磁盘加载模型"""
        safe_key = key.replace('/', '_').replace(':', '_')
        model_path = MODEL_DIR / f"{safe_key}.pt"
        meta_path = MODEL_DIR / f"{safe_key}.json"
        scaler_path = MODEL_DIR / f"{safe_key}_scaler.npz"

        if not model_path.exists() or not meta_path.exists():
            return False

        try:
            meta_dict = json.loads(meta_path.read_text())
            meta = ModelMeta(**meta_dict)

            import pickle
            with open(scaler_path, 'rb') as f:
                scaler = pickle.load(f)

            if meta.model_type == 'lstm':
                model = LSTMPredictor(
                    input_size=meta.n_features,
                    hidden_size=meta.hyperparams.get('hidden_size', 128),
                    num_layers=meta.hyperparams.get('num_layers', 2),
                    dropout=meta.hyperparams.get('dropout', 0.3),
                )
            elif meta.model_type == 'transformer':
                model = TransformerPredictor(
                    input_size=meta.n_features,
                    d_model=meta.hyperparams.get('d_model', 64),
                    nhead=meta.hyperparams.get('nhead', 4),
                    num_layers=meta.hyperparams.get('num_layers', 3),
                    dropout=meta.hyperparams.get('dropout', 0.2),
                )
            else:
                return False

            model.load_state_dict(torch.load(model_path, map_location=DEVICE, weights_only=True))
            model = model.to(DEVICE)
            model.eval()

            self._models[key] = model
            self._scalers[key] = scaler
            self._meta[key] = meta

            logger.info(f"Model loaded: {key} (val_acc={meta.val_accuracy:.4f})")
            return True

        except Exception as e:
            logger.error(f"Failed to load model {key}: {e}")
            return False

    def list_models(self) -> List[Dict]:
        """列出所有已训练的模型"""
        result = []
        for key, meta in self._meta.items():
            result.append({
                'key': key,
                'model_type': meta.model_type,
                'symbol': meta.symbol,
                'timeframe': meta.timeframe,
                'val_accuracy': round(meta.val_accuracy * 100, 1),
                'train_samples': meta.train_samples,
                'trained_at': meta.trained_at,
            })

        # 也扫描磁盘上的模型
        for pt_file in MODEL_DIR.glob("*.pt"):
            safe_key = pt_file.stem
            key = safe_key.replace('_', ':', 1).replace('_', '/', 1)
            if key not in self._meta:
                meta_file = MODEL_DIR / f"{safe_key}.json"
                if meta_file.exists():
                    try:
                        meta_dict = json.loads(meta_file.read_text())
                        result.append({
                            'key': key,
                            'model_type': meta_dict.get('model_type', ''),
                            'symbol': meta_dict.get('symbol', ''),
                            'timeframe': meta_dict.get('timeframe', ''),
                            'val_accuracy': round(meta_dict.get('val_accuracy', 0) * 100, 1),
                            'trained_at': meta_dict.get('trained_at', ''),
                            'on_disk': True,
                        })
                    except Exception:
                        pass

        return result


# ================================================================
# 全局单例
# ================================================================

model_manager = ModelManager()
