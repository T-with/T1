"""
engine/kill_switch.py — 紧急停止开关

功能:
1. 一键停止所有策略
2. 可选:同时平掉所有持仓 (实盘)
3. 写审计日志 (谁/何时/为何触发)
4. 激活后阻止新策略启动,直到人工解除
"""

import logging
import time
import threading
from typing import List, Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class KillSwitch:
    """
    全局紧急停止开关

    生命周期:
      - activate() → 所有策略停止 + 可选平仓 + 阻止新启动
      - is_active() → LiveTrader.start() 会检查
      - deactivate() → 人工解除
    """

    def __init__(self, audit_repo, risk_event_repo):
        self.audit_repo = audit_repo
        self.risk_event_repo = risk_event_repo
        self._active = False
        self._activated_at = 0.0
        self._reason = ''
        self._activated_by = ''
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> Dict:
        return {
            'active': self._active,
            'activated_at': self._activated_at,
            'activated_by': self._activated_by,
            'reason': self._reason,
            'elapsed_sec': time.time() - self._activated_at if self._active else 0,
        }

    # ----------------------------------------------------------

    def activate(self, reason: str, triggered_by: str = 'manual',
                 live_trader=None, flat_all_positions: bool = False,
                 make_exchange_client=None) -> Dict:
        """
        触发紧急停止

        Args:
            reason: 原因字符串
            triggered_by: 'manual' / 'risk_mgr' / 'api'
            live_trader: LiveTrader 实例
            flat_all_positions: 是否平掉所有实盘持仓
            make_exchange_client: 交易所客户端工厂 (flat_all_positions=True 时需要)
        """
        with self._lock:
            if self._active:
                return {
                    'ok': False,
                    'message': 'Kill switch already active',
                    'status': self.status(),
                }

            self._active = True
            self._activated_at = time.time()
            self._reason = reason
            self._activated_by = triggered_by

        logger.critical(f"🚨 KILL SWITCH ACTIVATED by {triggered_by}: {reason}")

        result = {
            'ok': True,
            'activated_at': self._activated_at,
            'strategies_stopped': [],
            'positions_closed': [],
            'errors': [],
        }

        # 1. 停止所有策略
        if live_trader:
            for sid in list(live_trader._threads.keys()):
                try:
                    live_trader.stop(sid)
                    result['strategies_stopped'].append(sid)
                except Exception as e:
                    result['errors'].append(f"stop {sid}: {e}")

        # 2. 可选: 平掉所有实盘持仓
        if flat_all_positions and make_exchange_client:
            if live_trader:
                for sid, state in list(live_trader._strategies.items()):
                    config = state.get('config')
                    if not config or config.paper or not config.api_key:
                        continue
                    for sym, pos in list(state.get('positions', {}).items()):
                        try:
                            client = make_exchange_client(config.exchange_id)
                            close_side = 'sell' if pos['side'] == 'long' else 'buy'
                            client.create_market_order(
                                config.symbol, close_side, pos['size']
                            )
                            result['positions_closed'].append({
                                'strategy': sid, 'symbol': sym, 'side': pos['side'],
                                'size': pos['size'],
                            })
                            logger.warning(
                                f"[kill_switch] Flatted {sid} {sym} "
                                f"{pos['side']} {pos['size']}"
                            )
                        except Exception as e:
                            result['errors'].append(
                                f"close {sid} {sym}: {e}"
                            )

        # 3. 落盘
        self.audit_repo.log(
            action='kill_switch_activated',
            target='global',
            user=triggered_by,
            details={
                'reason': reason,
                'flat_all': flat_all_positions,
                'strategies_stopped': result['strategies_stopped'],
                'positions_closed': result['positions_closed'],
                'errors': result['errors'],
            },
        )
        self.risk_event_repo.log({
            'event_type': 'kill_switch',
            'level': 'critical',
            'message': f'Kill switch activated: {reason}',
            'action_taken': 'halt_all',
            'data': result,
        })

        return result

    def deactivate(self, by: str = 'manual') -> Dict:
        """人工解除紧急停止"""
        with self._lock:
            if not self._active:
                return {'ok': False, 'message': 'Kill switch not active'}
            self._active = False
            elapsed = time.time() - self._activated_at

        logger.warning(f"Kill switch deactivated by {by} after {elapsed:.0f}s")

        self.audit_repo.log(
            action='kill_switch_deactivated',
            target='global',
            user=by,
            details={'elapsed_sec': round(elapsed, 0),
                     'previous_reason': self._reason},
        )

        return {
            'ok': True,
            'elapsed_sec': round(elapsed, 0),
            'previous_reason': self._reason,
        }
