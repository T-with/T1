"""
tests/test_phase2.py — Phase 2 功能测试

运行: pytest tests/test_phase2.py -v
"""
import pytest
import tempfile
from pathlib import Path
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """每个测试用独立 DB"""
    db_path = tmp_path / 'test.db'
    monkeypatch.setenv('TEST_DB_PATH', str(db_path))

    # 因为 storage 是 module-level singleton,要强制重新导入
    for mod in list(sys.modules):
        if mod.startswith('engine.storage') or mod.startswith('engine.reconciliation') \
                or mod.startswith('engine.kill_switch'):
            del sys.modules[mod]

    import importlib
    from engine import storage
    importlib.reload(storage)
    # 替换 DB 实例
    storage.db = storage.Database(db_path=db_path)
    for name in ('strategy_repo', 'exchange_repo', 'trade_repo',
                 'position_repo', 'risk_event_repo', 'audit_repo'):
        cls = {
            'strategy_repo': storage.StrategyRepo,
            'exchange_repo': storage.ExchangeConfigRepo,
            'trade_repo': storage.TradeRepo,
            'position_repo': storage.PositionRepo,
            'risk_event_repo': storage.RiskEventRepo,
            'audit_repo': storage.AuditRepo,
        }[name]
        setattr(storage, name, cls(storage.db))

    yield storage


class TestStorage:
    def test_strategy_crud(self, isolated_db):
        repo = isolated_db.strategy_repo
        repo.upsert('s1', {'name': 'Test', 'symbol': 'BTC/USDT',
                           'timeframe': '1h', 'type': 'macd_cross'})
        result = repo.list_all()
        assert 's1' in result
        assert result['s1']['name'] == 'Test'

        repo.update_status('s1', 'running')
        assert repo.get('s1')['status'] == 'running'

        repo.delete('s1')
        assert repo.get('s1') is None

    def test_trade_stats(self, isolated_db):
        repo = isolated_db.trade_repo
        for pnl in [100, -50, 200, -30, 150]:
            repo.insert({
                'strategy_id': 's1', 'symbol': 'BTC/USDT',
                'side': 'long', 'type': 'take_profit' if pnl > 0 else 'stop_loss',
                'pnl': pnl, 'pnl_pct': pnl / 100,
                'closed_at': '2025-01-01T10:00:00',
            })
        stats = repo.stats_for_strategy('s1')
        assert stats['total'] == 5
        assert stats['wins'] == 3
        assert stats['losses'] == 2

    def test_position_upsert(self, isolated_db):
        repo = isolated_db.position_repo
        repo.upsert('s1', 'BTC/USDT', {
            'side': 'long', 'size': 0.1, 'entry_price': 50000,
            'current_price': 50100, 'opened_at': 'now',
        })
        positions = repo.for_strategy('s1')
        assert 'BTC/USDT' in positions
        assert positions['BTC/USDT']['size'] == 0.1

        # Update should preserve highest price
        repo.upsert('s1', 'BTC/USDT', {
            'side': 'long', 'size': 0.1, 'entry_price': 50000,
            'current_price': 50500, 'opened_at': 'now',
        })
        positions = repo.for_strategy('s1')
        assert positions['BTC/USDT']['highest_price'] >= 50500


class TestReconciliation:
    def test_both_empty_no_report(self, isolated_db):
        from engine.reconciliation import PositionReconciler

        reconciler = PositionReconciler(
            isolated_db.position_repo,
            isolated_db.risk_event_repo,
        )

        class Cfg:
            id = 't1'; symbol = 'BTC/USDT'; paper = False; api_key = 'fake'

        class Ex:
            def fetch_positions(self, symbols=None): return []

        reports = reconciler.reconcile_strategy(Cfg(), Ex())
        assert reports == []

    def test_side_mismatch_halts(self, isolated_db):
        from engine.reconciliation import PositionReconciler

        isolated_db.position_repo.upsert('t1', 'BTC/USDT', {
            'side': 'long', 'size': 0.1, 'entry_price': 50000,
            'current_price': 50000, 'opened_at': 'now',
        })

        class Cfg:
            id = 't1'; symbol = 'BTC/USDT'; paper = False; api_key = 'fake'

        class Ex:
            def fetch_positions(self, symbols=None):
                return [{'symbol': 'BTC/USDT', 'contracts': -0.1, 'entryPrice': 50000}]

        reconciler = PositionReconciler(
            isolated_db.position_repo, isolated_db.risk_event_repo
        )
        reports = reconciler.reconcile_strategy(Cfg(), Ex())
        assert reports[0].status == 'side_mismatch'
        assert reports[0].action_recommended == 'halt'

    def test_paper_mode_skips_remote(self, isolated_db):
        from engine.reconciliation import PositionReconciler

        isolated_db.position_repo.upsert('t1', 'BTC/USDT', {
            'side': 'long', 'size': 0.1, 'entry_price': 50000,
            'current_price': 50000, 'opened_at': 'now',
        })

        class Cfg:
            id = 't1'; symbol = 'BTC/USDT'; paper = True; api_key = ''

        class Ex:
            def fetch_positions(self, symbols=None):
                raise Exception("should not be called")

        reconciler = PositionReconciler(
            isolated_db.position_repo, isolated_db.risk_event_repo
        )
        reports = reconciler.reconcile_strategy(Cfg(), Ex())
        assert reports[0].status == 'ok'


class TestKillSwitch:
    def test_activate_deactivate(self, isolated_db):
        from engine.kill_switch import KillSwitch

        ks = KillSwitch(isolated_db.audit_repo, isolated_db.risk_event_repo)

        class FakeTrader:
            _threads = {'s1': None, 's2': None}
            _strategies = {}
            def stop(self, sid): self._threads.pop(sid, None)

        result = ks.activate('test', triggered_by='test', live_trader=FakeTrader())
        assert result['ok'] is True
        assert ks.active is True
        assert len(result['strategies_stopped']) == 2

        # 再次 activate 应拒绝
        result2 = ks.activate('again', live_trader=FakeTrader())
        assert result2['ok'] is False

        ks.deactivate()
        assert ks.active is False

    def test_audit_log(self, isolated_db):
        from engine.kill_switch import KillSwitch

        ks = KillSwitch(isolated_db.audit_repo, isolated_db.risk_event_repo)
        ks.activate('test1', triggered_by='admin')
        ks.deactivate(by='admin')

        audit = isolated_db.audit_repo.recent(10)
        actions = [e['action'] for e in audit]
        assert 'kill_switch_activated' in actions
        assert 'kill_switch_deactivated' in actions
