"""
MyTradingPlatform — 数据层（兼容入口）

底层已升级为 WebSocket 实时引擎 (data_ws.py)
此文件保持向后兼容，app.py 不需要修改
"""

# 从新引擎导入所有公共接口
from engine.data_ws import (
    # 数据结构
    DataType, OrderBookLevel, OrderBook, Ticker, Trade, Liquidation,
    # 管理器
    KlineManager, OrderBookManager, TradeStreamManager,
    # 引擎
    RealtimeDataEngine, get_engine,
    # 兼容层
    DataManager, CompatFeed,
    # 事件总线
    EventBus,
)

# 向后兼容单例
from engine.data_ws import data_manager

__all__ = [
    'DataType', 'OrderBookLevel', 'OrderBook', 'Ticker', 'Trade', 'Liquidation',
    'KlineManager', 'OrderBookManager', 'TradeStreamManager',
    'RealtimeDataEngine', 'get_engine',
    'DataManager', 'CompatFeed',
    'EventBus',
    'data_manager',
]
