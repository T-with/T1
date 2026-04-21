"""
engine/metrics.py — Prometheus 指标

分 4 组:
- 系统指标: HTTP 请求数/延迟、Python GC、进程内存
- 业务指标: 策略运行数、权益、持仓、订单、PnL
- 风控指标: 熔断次数、风控事件数、对账失败数
- 外部调用: 交易所 API 延迟、LLM 调用延迟

使用:
    from engine.metrics import metrics
    metrics.orders_total.labels(strategy='s1', side='buy', status='filled').inc()
    with metrics.exchange_api_duration.labels(exchange='binance', endpoint='fetch_ohlcv').time():
        data = exchange.fetch_ohlcv(...)
"""

import time
import threading
from typing import Callable
from prometheus_client import (
    Counter, Gauge, Histogram, Summary, Info,
    CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
    REGISTRY,
)
from prometheus_client.multiprocess import MultiProcessCollector


class Metrics:
    """所有指标的集中定义 — 单例"""

    def __init__(self, registry: CollectorRegistry = None):
        self.registry = registry or REGISTRY

        # ============================================================
        # 系统/HTTP
        # ============================================================
        self.http_requests_total = Counter(
            'http_requests_total',
            'HTTP 请求总数',
            ['method', 'path', 'status'],
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            'http_request_duration_seconds',
            'HTTP 请求耗时',
            ['method', 'path'],
            # 关注 50ms/200ms/1s/5s 这些阈值
            buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30),
            registry=self.registry,
        )
        self.app_info = Info(
            'trading_platform',
            '应用基本信息',
            registry=self.registry,
        )

        # ============================================================
        # 策略
        # ============================================================
        self.strategies_running = Gauge(
            'strategies_running',
            '当前运行中的策略数',
            ['paper'],     # paper="true"/"false"
            registry=self.registry,
        )
        self.strategy_equity = Gauge(
            'strategy_equity',
            '策略当前权益 (USDT)',
            ['strategy_id', 'symbol'],
            registry=self.registry,
        )
        self.strategy_peak_equity = Gauge(
            'strategy_peak_equity',
            '策略历史最高权益',
            ['strategy_id'],
            registry=self.registry,
        )
        self.strategy_drawdown_pct = Gauge(
            'strategy_drawdown_pct',
            '策略当前回撤百分比',
            ['strategy_id'],
            registry=self.registry,
        )
        self.strategy_open_positions = Gauge(
            'strategy_open_positions',
            '策略当前持仓数',
            ['strategy_id', 'symbol', 'side'],
            registry=self.registry,
        )
        self.strategy_unrealized_pnl_pct = Gauge(
            'strategy_unrealized_pnl_pct',
            '未实现盈亏百分比',
            ['strategy_id', 'symbol'],
            registry=self.registry,
        )

        # ============================================================
        # 交易
        # ============================================================
        self.trades_total = Counter(
            'trades_total',
            '交易次数',
            ['strategy_id', 'side', 'result'],   # result=win/loss/break_even
            registry=self.registry,
        )
        self.trade_pnl = Histogram(
            'trade_pnl_usdt',
            '单笔交易盈亏 (USDT)',
            ['strategy_id'],
            buckets=(-1000, -500, -200, -100, -50, -20, -10, -5, 0,
                     5, 10, 20, 50, 100, 200, 500, 1000),
            registry=self.registry,
        )
        self.trade_pnl_pct = Histogram(
            'trade_pnl_pct',
            '单笔交易盈亏百分比',
            ['strategy_id'],
            buckets=(-10, -5, -3, -2, -1, -0.5, 0, 0.5, 1, 2, 3, 5, 10, 20),
            registry=self.registry,
        )

        # ============================================================
        # 订单执行
        # ============================================================
        self.orders_total = Counter(
            'orders_total',
            '订单总数',
            ['exchange', 'side', 'type', 'status'],
            registry=self.registry,
        )
        self.order_slippage_pct = Histogram(
            'order_slippage_pct',
            '订单滑点百分比',
            ['exchange', 'type'],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 2.0),
            registry=self.registry,
        )
        self.order_fill_duration_seconds = Histogram(
            'order_fill_duration_seconds',
            '订单成交耗时',
            ['exchange', 'type'],
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 300),
            registry=self.registry,
        )

        # ============================================================
        # 风控
        # ============================================================
        self.risk_events_total = Counter(
            'risk_events_total',
            '风控事件总数',
            ['event_type', 'level'],
            registry=self.registry,
        )
        self.circuit_breaker_active = Gauge(
            'circuit_breaker_active',
            '全局熔断状态 (1=激活,0=正常)',
            registry=self.registry,
        )
        self.kill_switch_active = Gauge(
            'kill_switch_active',
            'Kill Switch 状态',
            registry=self.registry,
        )
        self.halted_strategies = Gauge(
            'halted_strategies',
            '被暂停的策略数 (连续亏损触发)',
            registry=self.registry,
        )
        self.reconcile_drift_total = Counter(
            'reconcile_drift_total',
            '持仓对账偏差事件',
            ['strategy_id', 'drift_type'],
            registry=self.registry,
        )

        # ============================================================
        # 外部调用
        # ============================================================
        self.exchange_api_duration = Histogram(
            'exchange_api_duration_seconds',
            '交易所 API 调用耗时',
            ['exchange', 'endpoint'],
            buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30),
            registry=self.registry,
        )
        self.exchange_api_errors_total = Counter(
            'exchange_api_errors_total',
            '交易所 API 错误',
            ['exchange', 'endpoint', 'error_type'],
            registry=self.registry,
        )
        self.llm_calls_total = Counter(
            'llm_calls_total',
            'LLM 调用次数',
            ['provider', 'model', 'status'],
            registry=self.registry,
        )
        self.llm_call_duration = Histogram(
            'llm_call_duration_seconds',
            'LLM 调用耗时',
            ['provider', 'model'],
            buckets=(0.5, 1, 2, 5, 10, 30, 60),
            registry=self.registry,
        )

        # ============================================================
        # 模型训练
        # ============================================================
        self.model_train_duration_seconds = Histogram(
            'model_train_duration_seconds',
            '模型训练耗时',
            ['model_type', 'symbol'],
            buckets=(10, 30, 60, 120, 300, 600, 1800),
            registry=self.registry,
        )
        self.model_val_accuracy = Gauge(
            'model_val_accuracy',
            '模型验证准确率',
            ['model_type', 'symbol', 'timeframe'],
            registry=self.registry,
        )

        # ============================================================
        # 告警
        # ============================================================
        self.alerts_sent_total = Counter(
            'alerts_sent_total',
            '告警发送总数',
            ['channel', 'severity', 'status'],
            registry=self.registry,
        )

    # ============================================================
    # Helpers
    # ============================================================

    def track_http(self, method: str, path: str, status: int, duration_sec: float):
        # 把路径归一化 — /api/strategies/abc123 → /api/strategies/:id
        self.http_requests_total.labels(method, self._normalize_path(path), str(status)).inc()
        self.http_request_duration_seconds.labels(method, self._normalize_path(path)).observe(duration_sec)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """把路径最后一段如果像 id/symbol 则替换成占位符,避免标签爆炸

        例如:
        - /api/strategies/abc12345  → /api/strategies/:id
        - /api/sentiment/BTC_USDT   → /api/sentiment/:symbol
        - /api/kline/ETH_USDT       → /api/kline/:symbol
        - /api/health               → /api/health  (不变)
        """
        import re
        parts = path.split('/')
        if not parts:
            return path
        last = parts[-1]
        # 交易对模式: BTC_USDT
        if re.fullmatch(r'[A-Z]{2,}_[A-Z]{2,}', last):
            parts[-1] = ':symbol'
        # id 模式: 6+ 位字母数字混合 (含数字才算 id,避免误伤 'strategies' 等)
        elif (len(last) >= 6 and last.isalnum()
              and any(c.isdigit() for c in last)):
            parts[-1] = ':id'
        return '/'.join(parts)

    def snapshot_from_live_trader(self, live_trader):
        """
        从 LiveTrader 快照更新 Gauge 类指标 — 定期调用

        这比每次事件实时更新省 CPU,也更符合 Prometheus 语义
        """
        try:
            running_paper = 0
            running_real = 0
            halted = set()
            for sid, state in live_trader._strategies.items():
                config = state.get('config')
                if not config:
                    continue
                if config.status == 'running':
                    if config.paper:
                        running_paper += 1
                    else:
                        running_real += 1

                self.strategy_equity.labels(sid, config.symbol).set(state.get('equity', 0))
                peak = state.get('peak_equity', state.get('equity', 0))
                self.strategy_peak_equity.labels(sid).set(peak)
                if peak > 0:
                    dd = (peak - state.get('equity', 0)) / peak * 100
                    self.strategy_drawdown_pct.labels(sid).set(dd)

                # 清理旧的 position gauges 然后重新填
                positions = state.get('positions', {})
                for sym, pos in positions.items():
                    self.strategy_open_positions.labels(sid, sym, pos['side']).set(1)
                    self.strategy_unrealized_pnl_pct.labels(sid, sym).set(pos.get('pnl_pct', 0))

            self.strategies_running.labels(paper='true').set(running_paper)
            self.strategies_running.labels(paper='false').set(running_real)
        except Exception:
            pass   # 指标采集绝不能影响主流程


# ============================================================
# 全局单例 + 周期性快照线程
# ============================================================

metrics = Metrics()


def start_metrics_snapshot_thread(live_trader, risk_manager, interval: int = 15):
    """启动后台线程,定期把内存状态同步到 Gauge 指标"""
    def _loop():
        import logging
        logger = logging.getLogger('metrics.snapshot')
        while True:
            try:
                metrics.snapshot_from_live_trader(live_trader)
                # 风控状态
                metrics.circuit_breaker_active.set(
                    1 if risk_manager._circuit_breaker_active else 0
                )
                metrics.halted_strategies.set(len(risk_manager._halted_strategies))
            except Exception as e:
                logger.debug(f"metrics snapshot error: {e}")
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name='metrics-snapshot')
    t.start()
    return t
