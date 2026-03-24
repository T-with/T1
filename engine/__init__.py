"""MyTradingPlatform Engine"""
from .core import (
    StrategyConfig, ExchangeClient, BacktestEngine,
    LiveTrader, Indicators, StrategyEngine, SignalType,
    Trade, Position
)
from .data import (
    DataManager, WebSocketFeed, OrderBookManager,
    KlineManager, TradeStreamManager, OrderBook,
    Ticker, Trade as StreamTrade, data_manager
)
from .execution import (
    SmartOrderRouter, TWAPExecutor, VWAPExecutor,
    OrderRequest, OrderResult, OrderType, OrderStatus,
    SlippageEstimator
)
from .risk import (
    RiskManager, KellyPositionSizer, AdvancedTrailingStop,
    VolatilityEngine, RiskLevel, RiskEventType, RiskEvent,
    risk_manager
)
