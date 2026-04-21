"""
engine/reconciliation.py — 持仓对账器

解决三个场景:
1. 程序重启 — 从 DB 恢复内存状态,从交易所 fetch_positions 核对
2. 策略暂停期间 — 交易所可能有手动交易,恢复时检测偏差
3. 订单失败 — 下单超时但实际成交,avoids 双倍开仓

工作流:
   a. LiveTrader 启动策略时调用 reconcile()
   b. 比对 DB.positions ↔ exchange.fetch_positions
   c. 对不上的产生 RiskEvent,停止自动交易等人工处理
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationReport:
    strategy_id: str
    symbol: str
    status: str                     # 'ok' / 'drift' / 'missing_local' / 'missing_remote' / 'size_mismatch' / 'side_mismatch'
    local_position: Optional[dict] = None
    remote_position: Optional[dict] = None
    diff: Dict = field(default_factory=dict)
    action_recommended: str = 'none'   # 'none' / 'halt' / 'sync_local' / 'manual_review'
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            'strategy_id': self.strategy_id,
            'symbol': self.symbol,
            'status': self.status,
            'local_position': self.local_position,
            'remote_position': self.remote_position,
            'diff': self.diff,
            'action_recommended': self.action_recommended,
            'timestamp': self.timestamp,
        }


class PositionReconciler:
    """
    持仓对账器

    使用:
        reconciler = PositionReconciler(position_repo, risk_event_repo)
        reports = reconciler.reconcile_strategy(config, exchange_client)
        for r in reports:
            if r.status != 'ok':
                logger.warning(f'Drift: {r.to_dict()}')
    """

    # 视为相等的数量误差阈值 (小数点后误差)
    SIZE_TOLERANCE_PCT = 0.5

    def __init__(self, position_repo, risk_event_repo):
        self.position_repo = position_repo
        self.risk_event_repo = risk_event_repo

    # ----------------------------------------------------------

    def restore_from_db(self, strategy_id: str) -> Dict[str, dict]:
        """从 DB 读回内存需要的持仓结构"""
        rows = self.position_repo.for_strategy(strategy_id)
        result = {}
        for sym, r in rows.items():
            result[sym] = {
                'side': r['side'],
                'size': r['size'],
                'entry_price': r['entry_price'],
                'current_price': r.get('current_price', r['entry_price']),
                'opened_at': r['opened_at'],
                'highest_price': r.get('highest_price', r['entry_price']),
                'lowest_price': r.get('lowest_price', r['entry_price']),
            }
        if result:
            logger.info(f"Restored {len(result)} positions for strategy {strategy_id} from DB")
        return result

    def save_to_db(self, strategy_id: str, positions: Dict[str, dict]):
        """持仓变更时同步写 DB"""
        for sym, pos in positions.items():
            self.position_repo.upsert(strategy_id, sym, pos)

    def clear_position(self, strategy_id: str, symbol: str):
        self.position_repo.delete(strategy_id, symbol)

    # ----------------------------------------------------------

    def reconcile_strategy(self, config, exchange_client) -> List[ReconciliationReport]:
        """
        对账单个策略所有持仓

        Args:
            config: StrategyConfig 实例
            exchange_client: ExchangeClient 实例 (有 fetch_positions 方法)

        Returns:
            每个涉及的 symbol 一份 Report
        """
        reports = []

        # 1. 获取本地持仓 (DB)
        local = self.position_repo.for_strategy(config.id)

        # 2. 获取远端持仓 — 纸面交易不需要对账
        if config.paper:
            # 纸面模式下,直接信任本地
            for sym, pos in local.items():
                reports.append(ReconciliationReport(
                    strategy_id=config.id, symbol=sym, status='ok',
                    local_position=pos, action_recommended='none',
                    timestamp=time.time(),
                ))
            return reports

        if not config.api_key:
            logger.warning(f"Skip reconcile for {config.id}: no API key configured")
            return reports

        try:
            remote_positions_list = exchange_client.fetch_positions([config.symbol])
        except Exception as e:
            logger.error(f"fetch_positions failed for {config.id}: {e}")
            # 无法对账时,产生 warning 但不阻止启动
            report = ReconciliationReport(
                strategy_id=config.id, symbol=config.symbol,
                status='error', diff={'fetch_error': str(e)},
                action_recommended='manual_review', timestamp=time.time(),
            )
            self._log_drift_event(report)
            return [report]

        remote = self._parse_remote(remote_positions_list, config.symbol)

        # 3. 对比本地 vs 远端
        all_symbols = set(local.keys()) | set(remote.keys())
        for sym in all_symbols:
            report = self._compare(config.id, sym, local.get(sym), remote.get(sym))
            reports.append(report)
            if report.status != 'ok':
                self._log_drift_event(report)

        return reports

    # ----------------------------------------------------------

    def _parse_remote(self, positions_list: list, target_symbol: str) -> Dict[str, dict]:
        """ccxt fetch_positions 返回结构标准化"""
        result = {}
        for pos in positions_list or []:
            sym = pos.get('symbol', '')
            if sym != target_symbol and sym.replace(':USDT', '') != target_symbol:
                continue
            size = float(pos.get('contracts', 0) or pos.get('amount', 0) or 0)
            if abs(size) < 1e-9:
                continue
            side = 'long' if size > 0 else 'short'
            result[sym] = {
                'side': side,
                'size': abs(size),
                'entry_price': float(pos.get('entryPrice', 0) or 0),
                'unrealized_pnl': float(pos.get('unrealizedPnl', 0) or 0),
            }
        return result

    def _compare(self, strategy_id: str, symbol: str,
                 local: Optional[dict], remote: Optional[dict]) -> ReconciliationReport:
        now = time.time()

        # Case 1: 双方都没有 → OK
        if local is None and remote is None:
            return ReconciliationReport(
                strategy_id=strategy_id, symbol=symbol, status='ok',
                timestamp=now,
            )

        # Case 2: 本地有,远端没有 → 可能是订单失败或手动平仓
        if local and not remote:
            return ReconciliationReport(
                strategy_id=strategy_id, symbol=symbol, status='missing_remote',
                local_position=local,
                diff={'message': '本地记录有持仓但交易所无持仓 (可能订单未成交或手动平仓)'},
                action_recommended='sync_local',  # 建议清空本地记录
                timestamp=now,
            )

        # Case 3: 远端有,本地没有 → 有外部持仓,可能是重启前开的单未落盘,或者手动开仓
        if remote and not local:
            return ReconciliationReport(
                strategy_id=strategy_id, symbol=symbol, status='missing_local',
                remote_position=remote,
                diff={'message': '交易所有持仓但本地无记录 — 禁止自动交易,请人工处理'},
                action_recommended='halt',        # 高危! 必须停策略
                timestamp=now,
            )

        # Case 4: 两边都有 — 对比方向和数量
        if local['side'] != remote['side']:
            return ReconciliationReport(
                strategy_id=strategy_id, symbol=symbol, status='side_mismatch',
                local_position=local, remote_position=remote,
                diff={
                    'local_side': local['side'], 'remote_side': remote['side'],
                    'message': '方向不一致! 极高风险'
                },
                action_recommended='halt', timestamp=now,
            )

        size_diff_pct = abs(local['size'] - remote['size']) / max(local['size'], 1e-9) * 100
        if size_diff_pct > self.SIZE_TOLERANCE_PCT:
            return ReconciliationReport(
                strategy_id=strategy_id, symbol=symbol, status='size_mismatch',
                local_position=local, remote_position=remote,
                diff={
                    'local_size': local['size'],
                    'remote_size': remote['size'],
                    'diff_pct': round(size_diff_pct, 4),
                    'message': f'仓位数量偏差 {size_diff_pct:.2f}% (容忍 {self.SIZE_TOLERANCE_PCT}%)',
                },
                action_recommended='manual_review', timestamp=now,
            )

        # 数量和方向都对得上 → OK
        return ReconciliationReport(
            strategy_id=strategy_id, symbol=symbol, status='ok',
            local_position=local, remote_position=remote,
            timestamp=now,
        )

    def _log_drift_event(self, report: ReconciliationReport):
        """把漂移事件写入风控事件表"""
        level = 'critical' if report.action_recommended == 'halt' else 'warning'
        self.risk_event_repo.log({
            'timestamp': report.timestamp,
            'event_type': f'reconcile_{report.status}',
            'level': level,
            'strategy_id': report.strategy_id,
            'symbol': report.symbol,
            'message': report.diff.get('message', f'Position drift: {report.status}'),
            'data': report.to_dict(),
            'action_taken': report.action_recommended,
        })
        logger.warning(
            f"[reconcile] {report.strategy_id} {report.symbol} "
            f"status={report.status} action={report.action_recommended}"
        )
