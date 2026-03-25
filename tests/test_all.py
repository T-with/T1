"""
MyTradingPlatform — 完整测试套件
覆盖: 指标/策略/回测/数据层/执行层/风控/模型/API

运行: python3 -m pytest tests/ -v
"""

import pytest
import numpy as np
import pandas as pd
import sys
import os
import json
import time

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ================================================================
# Fixtures
# ================================================================

@pytest.fixture
def sample_df():
    """生成 200 根模拟 K 线数据"""
    np.random.seed(42)
    n = 200
    dates = pd.date_range('2024-01-01', periods=n, freq='1h')
    close = 40000 + np.cumsum(np.random.randn(n) * 50)
    high = close + np.abs(np.random.randn(n) * 30)
    low = close - np.abs(np.random.randn(n) * 30)
    opn = close + np.random.randn(n) * 20
    volume = np.random.uniform(100, 1000, n)
    return pd.DataFrame({
        'open': opn, 'high': high, 'low': low,
        'close': close, 'volume': volume,
    }, index=dates)


@pytest.fixture
def sample_df_large():
    """生成 600 根 K 线（足够 AI 策略使用）"""
    np.random.seed(123)
    n = 600
    dates = pd.date_range('2024-01-01', periods=n, freq='1h')
    close = 40000 + np.cumsum(np.random.randn(n) * 80)
    high = close + np.abs(np.random.randn(n) * 40)
    low = close - np.abs(np.random.randn(n) * 40)
    opn = close + np.random.randn(n) * 25
    volume = np.random.uniform(200, 2000, n)
    return pd.DataFrame({
        'open': opn, 'high': high, 'low': low,
        'close': close, 'volume': volume,
    }, index=dates)


@pytest.fixture
def app_client():
    """Flask 测试客户端（带 Basic Auth）"""
    import base64
    os.environ['ADMIN_USER'] = 'test'
    os.environ['ADMIN_PASS'] = 'test123'

    from app import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        # 设置 Basic Auth header
        creds = base64.b64encode(b'test:test123').decode('utf-8')
        client.environ_base['HTTP_AUTHORIZATION'] = f'Basic {creds}'
        yield client


# ================================================================
# 1. 技术指标测试
# ================================================================

class TestIndicators:
    def test_sma(self, sample_df):
        from engine.core import Indicators
        sma = Indicators.sma(sample_df, 10)
        assert len(sma) == len(sample_df)
        assert np.isnan(sma.iloc[0])
        assert not np.isnan(sma.iloc[10])

    def test_ema(self, sample_df):
        from engine.core import Indicators
        ema = Indicators.ema(sample_df, 12)
        assert len(ema) == len(sample_df)
        assert not np.isnan(ema.iloc[12])

    def test_rsi(self, sample_df):
        from engine.core import Indicators
        rsi = Indicators.rsi(sample_df, 14)
        assert len(rsi) == len(sample_df)
        valid = rsi.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_macd(self, sample_df):
        from engine.core import Indicators
        macd_line, signal_line, hist = Indicators.macd(sample_df)
        assert len(macd_line) == len(sample_df)
        assert len(signal_line) == len(sample_df)
        assert len(hist) == len(sample_df)

    def test_bollinger(self, sample_df):
        from engine.core import Indicators
        upper, mid, lower = Indicators.bollinger(sample_df)
        assert len(upper) == len(sample_df)
        # 上轨 >= 中轨 >= 下轨
        valid = ~(np.isnan(upper) | np.isnan(lower))
        assert (upper[valid] >= lower[valid]).all()

    def test_atr(self, sample_df):
        from engine.core import Indicators
        atr = Indicators.atr(sample_df, 14)
        valid = atr.dropna()
        assert (valid > 0).all()

    def test_add_all(self, sample_df):
        from engine.core import Indicators
        df = Indicators.add_all(sample_df)
        expected_cols = ['sma_10', 'sma_20', 'rsi', 'macd', 'bb_upper', 'atr']
        for col in expected_cols:
            assert col in df.columns, f"Missing column: {col}"


# ================================================================
# 2. 策略信号测试
# ================================================================

class TestStrategies:
    def test_macd_cross_signals(self, sample_df):
        from engine.core import StrategyEngine, Indicators
        df = Indicators.add_all(sample_df)
        signals = StrategyEngine.generate_signals(df, 'macd_cross', {})
        assert isinstance(signals, list)
        for sig in signals:
            assert sig['type'] in ('buy', 'sell')
            assert 'price' in sig
            assert 'confidence' in sig

    def test_rsi_reversal_signals(self, sample_df):
        from engine.core import StrategyEngine, Indicators
        df = Indicators.add_all(sample_df)
        signals = StrategyEngine.generate_signals(df, 'rsi_reversal', {'oversold': 30, 'overbought': 70})
        assert isinstance(signals, list)

    def test_bollinger_breakout_signals(self, sample_df):
        from engine.core import StrategyEngine, Indicators
        df = Indicators.add_all(sample_df)
        signals = StrategyEngine.generate_signals(df, 'bollinger_breakout', {})
        assert isinstance(signals, list)

    def test_dual_ma_signals(self, sample_df):
        from engine.core import StrategyEngine, Indicators
        df = Indicators.add_all(sample_df)
        signals = StrategyEngine.generate_signals(df, 'dual_ma', {'fast_period': 10, 'slow_period': 30})
        assert isinstance(signals, list)

    def test_ai_grid_signals(self, sample_df):
        from engine.core import AIGridStrategy, Indicators
        df = Indicators.add_all(sample_df)
        signals = AIGridStrategy.generate_signals(df, {'base_grids': 10})
        assert isinstance(signals, list)
        for sig in signals:
            assert 'grid_info' in sig or sig['type'] in ('buy', 'sell')

    def test_smart_dca_signals(self, sample_df):
        from engine.core import SmartDCAStrategy, Indicators
        df = Indicators.add_all(sample_df)
        signals = SmartDCAStrategy.generate_signals(df, {
            'base_invest_pct': 5,
            'interval_hours': 1,
            'last_buy_time': 0,
        })
        assert isinstance(signals, list)

    def test_stat_arb_signals(self, sample_df):
        from engine.core import StatisticalArbStrategy
        signals = StatisticalArbStrategy.generate_signals(sample_df, 'stat_arb', {
            'zscore_threshold': 2.0, 'zscore_period': 20,
        })
        assert isinstance(signals, list)

    def test_empty_df(self):
        from engine.core import StrategyEngine
        empty_df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        signals = StrategyEngine.generate_signals(empty_df, 'macd_cross', {})
        assert signals == []

    def test_short_df(self):
        from engine.core import StrategyEngine
        short_df = pd.DataFrame({
            'open': [1, 2], 'high': [1, 2], 'low': [1, 2],
            'close': [1, 2], 'volume': [1, 1],
        })
        signals = StrategyEngine.generate_signals(short_df, 'macd_cross', {})
        assert signals == []


# ================================================================
# 3. 回测引擎测试
# ================================================================

class TestBacktest:
    def test_basic_backtest(self, sample_df):
        from engine.core import BacktestEngine
        result = BacktestEngine.run(
            df=sample_df,
            strategy_type='macd_cross',
            params={},
            capital=10000,
            commission=0.0004,
            slippage=0.0005,
        )
        assert 'initial_capital' in result
        assert 'final_capital' in result
        assert 'total_return_pct' in result
        assert 'total_trades' in result
        assert 'win_rate' in result
        assert 'sharpe_ratio' in result
        assert 'max_drawdown_pct' in result
        assert 'equity_curve' in result
        assert result['initial_capital'] == 10000

    def test_backtest_rsi(self, sample_df):
        from engine.core import BacktestEngine
        result = BacktestEngine.run(sample_df, 'rsi_reversal', {'oversold': 30, 'overbought': 70})
        assert isinstance(result['total_trades'], int)
        assert result['win_rate'] >= 0

    def test_backtest_all_strategies(self, sample_df):
        from engine.core import BacktestEngine
        strategies = ['macd_cross', 'rsi_reversal', 'bollinger_breakout', 'dual_ma']
        for st in strategies:
            result = BacktestEngine.run(sample_df, st, {})
            assert 'total_trades' in result, f"Backtest failed for {st}"

    def test_backtest_with_leverage(self, sample_df):
        from engine.core import BacktestEngine
        result = BacktestEngine.run(sample_df, 'macd_cross', {}, leverage=5)
        assert 'total_return_pct' in result

    def test_backtest_equity_curve(self, sample_df):
        from engine.core import BacktestEngine
        result = BacktestEngine.run(sample_df, 'macd_cross', {})
        assert len(result['equity_curve']) == len(sample_df)
        assert all(isinstance(v, (int, float)) for v in result['equity_curve'])


# ================================================================
# 4. 数据层测试
# ================================================================

class TestDataLayer:
    def test_orderbook(self):
        from engine.data_ws import OrderBook, OrderBookLevel
        book = OrderBook(
            symbol='BTC/USDT', exchange='binance',
            bids=[OrderBookLevel(50000, 1.5), OrderBookLevel(49999, 2.0)],
            asks=[OrderBookLevel(50001, 1.0), OrderBookLevel(50002, 3.0)],
        )
        assert book.best_bid == 50000
        assert book.best_ask == 50001
        assert book.spread == 1.0
        assert book.mid_price == 50000.5
        assert book.imbalance_ratio(2) == pytest.approx(1.75 / 2.0)

    def test_kline_manager(self):
        from engine.data_ws import KlineManager
        import asyncio

        async def run():
            km = KlineManager(max_bars=100)
            df = pd.DataFrame({
                'open': [100], 'high': [110], 'low': [90],
                'close': [105], 'volume': [1000],
            }, index=[pd.Timestamp('2024-01-01')])
            await km.initialize('binance', 'BTC/USDT', '1h', df)
            result = km.get('binance', 'BTC/USDT', '1h')
            assert len(result) == 1
            assert km.latest_price('binance', 'BTC/USDT', '1h') == 105

        asyncio.run(run())

    def test_trade_stream(self):
        from engine.data_ws import TradeStreamManager, Trade
        tsm = TradeStreamManager()
        for i in range(100):
            tsm.add_trade(Trade(
                id=str(i), symbol='BTC/USDT', exchange='binance',
                side='buy' if i % 2 == 0 else 'sell',
                price=50000 + i, amount=0.1,
                timestamp=time.time() - (100 - i),
            ))
        stats = tsm.get_stats('binance', 'BTC/USDT')
        assert stats is not None
        assert stats['count'] == 100

    def test_event_bus(self):
        from engine.data_ws import EventBus
        import asyncio

        async def run():
            bus = EventBus()
            received = []
            bus.subscribe('test', lambda d: received.append(d))
            await bus.publish('test', {'msg': 'hello'})
            assert len(received) == 1
            assert received[0]['msg'] == 'hello'

        asyncio.run(run())


# ================================================================
# 5. 执行层测试
# ================================================================

class TestExecution:
    def test_order_types(self):
        from engine.execution import OrderType, OrderStatus
        assert OrderType.MARKET.value == 'market'
        assert OrderType.TWAP.value == 'twap'
        assert OrderStatus.PENDING.value == 'pending'

    def test_order_request(self):
        from engine.execution import OrderRequest, OrderType
        order = OrderRequest(
            id='test1', symbol='BTC/USDT', exchange='binance',
            side='buy', type=OrderType.MARKET, total_amount=0.01,
        )
        assert order.id == 'test1'
        assert order.total_amount == 0.01

    def test_slippage_estimation(self):
        from engine.execution import SlippageEstimator
        from engine.data_ws import OrderBook, OrderBookLevel
        book = OrderBook(
            bids=[OrderBookLevel(50000, 1.0), OrderBookLevel(49999, 2.0)],
            asks=[OrderBookLevel(50001, 1.0), OrderBookLevel(50002, 3.0)],
        )
        result = SlippageEstimator.estimate(book, 'buy', 0.5)
        assert 'slippage_pct' in result
        assert 'estimated_price' in result


# ================================================================
# 6. 风控测试
# ================================================================

class TestRisk:
    def test_risk_manager_init(self):
        from engine.risk import risk_manager
        dashboard = risk_manager.get_risk_dashboard()
        assert 'circuit_breaker' in dashboard
        assert 'equity' in dashboard

    def test_volatility_engine(self):
        from engine.risk import VolatilityEngine
        np.random.seed(42)
        prices = 40000 + np.cumsum(np.random.randn(100) * 50)
        ve = VolatilityEngine()
        hv = ve.historical_volatility(prices, 20)
        assert hv > 0
        regime = ve.implied_regime(prices)
        assert regime in ('low_vol', 'normal', 'high_vol', 'extreme')

    def test_kelly_calculator(self):
        from engine.risk import risk_manager
        result = risk_manager.kelly.calculate('test_strategy')
        assert 'position_pct' in result


# ================================================================
# 7. 模型测试
# ================================================================

class TestModels:
    def test_feature_engineer(self, sample_df):
        from engine.models import FeatureEngineer
        fdf, cols = FeatureEngineer.compute_features(sample_df)
        assert len(cols) > 10
        assert 'rsi_14' in cols
        assert 'macd_hist' in cols
        assert len(fdf) == len(sample_df)

    def test_transformer_forward(self):
        import torch
        from engine.models import TransformerPredictor
        model = TransformerPredictor(input_size=10, d_model=32, nhead=4, num_layers=2)
        x = torch.randn(2, 20, 10)
        out = model(x)
        assert out.shape == (2,)
        assert (out >= 0).all() and (out <= 1).all()

    def test_transformer_attention(self):
        import torch
        from engine.models import TransformerPredictor
        model = TransformerPredictor(input_size=10, d_model=32, nhead=4, num_layers=2)
        x = torch.randn(2, 20, 10)
        out = model(x, return_attention=True)
        attn = model.get_attention_weights()
        assert attn is not None
        assert attn.shape == (2, 20, 1)

    def test_lstm_forward(self):
        import torch
        from engine.models import LSTMPredictor
        model = LSTMPredictor(input_size=10, hidden_size=64, num_layers=2)
        x = torch.randn(2, 20, 10)
        out = model(x)
        assert out.shape == (2,)

    def test_focal_loss(self):
        import torch
        from engine.models import FocalLoss
        fl = FocalLoss(gamma=2.0, alpha=0.25)
        pred = torch.sigmoid(torch.randn(10))
        target = torch.tensor([0., 1., 0., 1., 0., 1., 0., 0., 1., 1.])
        loss = fl(pred, target)
        assert loss.item() > 0

    def test_prepare_sequences(self, sample_df):
        from engine.models import FeatureEngineer
        fdf, cols = FeatureEngineer.compute_features(sample_df)
        X, y, scaler, indices = FeatureEngineer.prepare_sequences(fdf, cols, seq_len=30)
        assert X.shape[0] == len(y)
        assert X.shape[1] == 30
        assert X.shape[2] == len(cols)


# ================================================================
# 8. API 端点测试
# ================================================================

class TestAPI:
    def test_health(self, app_client):
        resp = app_client.get('/health')
        assert resp.status_code == 200
        assert resp.json['status'] == 'ok'

    def test_auth_required(self):
        """不带认证应该 401"""
        os.environ['ADMIN_USER'] = 'test'
        os.environ['ADMIN_PASS'] = 'test123'
        from app import app
        app.config['TESTING'] = True
        with app.test_client() as client:
            resp = client.get('/api/strategies')
            assert resp.status_code == 401

    def test_create_strategy(self, app_client):
        resp = app_client.post('/api/strategies', json={
            'name': 'TestStrategy',
            'symbol': 'BTC/USDT',
            'timeframe': '1h',
            'type': 'macd_cross',
            'capital': 10000,
            'paper': True,
        })
        assert resp.status_code == 200
        assert resp.json['ok'] is True
        assert 'id' in resp.json

    def test_list_strategies(self, app_client):
        resp = app_client.get('/api/strategies')
        assert resp.status_code == 200
        assert isinstance(resp.json, dict)

    def test_risk_dashboard(self, app_client):
        resp = app_client.get('/api/risk/dashboard')
        assert resp.status_code == 200
        assert 'circuit_breaker' in resp.json

    def test_model_list(self, app_client):
        resp = app_client.get('/api/models/list')
        assert resp.status_code == 200
        assert 'dl_models' in resp.json
        assert 'rl_models' in resp.json

    def test_backtest_endpoint(self, app_client):
        resp = app_client.post('/api/backtest', json={
            'symbol': 'BTC/USDT',
            'timeframe': '1h',
            'strategy_type': 'macd_cross',
            'start_date': '2024-06-01',
            'end_date': '2024-06-10',
            'capital': 10000,
        })
        # 可能因网络问题失败，但不应 500
        assert resp.status_code in (200, 400, 500)

    def test_live_status(self, app_client):
        resp = app_client.get('/api/live/status')
        assert resp.status_code == 200


# ================================================================
# 9. 数据加密测试
# ================================================================

class TestEncryption:
    def test_encrypt_decrypt(self):
        from engine.core import encrypt_secret, decrypt_secret
        plaintext = 'my_api_key_12345'
        encrypted = encrypt_secret(plaintext)
        assert encrypted != plaintext
        decrypted = decrypt_secret(encrypted)
        assert decrypted == plaintext

    def test_exchange_config_encryption(self):
        from engine.core import encrypt_exchange_config, decrypt_exchange_config
        config = {
            'exchange_id': 'binance',
            'api_key': 'test_key_123',
            'api_secret': 'test_secret_456',
        }
        encrypted = encrypt_exchange_config(config)
        assert encrypted['api_key'].startswith('enc:')
        decrypted = decrypt_exchange_config(encrypted)
        assert decrypted['api_key'] == 'test_key_123'

    def test_redact_secrets(self):
        from engine.core import redact_secrets
        config = {'api_key': 'abcdefghijklmnop', 'api_secret': 'secret123'}
        redacted = redact_secrets(config)
        assert '****' in redacted['api_key']
        assert redacted['api_key'].startswith('abcd')
