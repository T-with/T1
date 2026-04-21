"""
tests/test_phase3.py — Phase 3 观测性测试

运行: pytest tests/test_phase3.py -v
"""
import pytest
import time
import sys, os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestLogging:
    def test_json_rendering(self, capsys):
        from engine.logging_setup import setup_logging, get_logger
        setup_logging(level='DEBUG', json_mode=True)
        log = get_logger('test')
        log.info('hello', foo='bar')
        import json
        captured = capsys.readouterr()
        # 最后一行应该是合法 JSON
        last = [l for l in captured.out.strip().split('\n') if l.strip().startswith('{')][-1]
        obj = json.loads(last)
        assert obj['event'] == 'hello'
        assert obj['foo'] == 'bar'
        assert 'timestamp' in obj
        assert obj['service'] == 'trading-platform'

    def test_sensitive_redaction(self, capsys):
        from engine.logging_setup import setup_logging, get_logger
        setup_logging(level='DEBUG', json_mode=True)
        log = get_logger('test')
        log.info('config_loaded',
                 api_key='sk-abcdefghijklmnop',
                 api_secret='supersecrettoken')
        import json
        captured = capsys.readouterr()
        last = [l for l in captured.out.strip().split('\n') if l.strip().startswith('{')][-1]
        obj = json.loads(last)
        assert '****' in obj['api_key']
        assert 'sk-a' in obj['api_key']
        assert 'mnop' in obj['api_key']
        assert 'supersecret' not in obj['api_secret']

    def test_context_binding(self, capsys):
        from engine.logging_setup import setup_logging, get_logger, LogContext
        setup_logging(level='DEBUG', json_mode=True)
        log = get_logger('test')
        with LogContext(strategy_id='s1', trade_id=42):
            log.warning('inside_context', ok=True)
        log.info('outside_context')   # 不应带 strategy_id

        import json
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split('\n') if l.strip().startswith('{')]
        inside = next(json.loads(l) for l in lines if 'inside_context' in l)
        outside = next(json.loads(l) for l in lines if 'outside_context' in l)
        assert inside.get('strategy_id') == 's1'
        assert inside.get('trade_id') == 42
        assert 'strategy_id' not in outside


class TestMetrics:
    def test_counters_and_gauges(self):
        from engine.metrics import metrics
        metrics.trades_total.labels('test_s1', 'buy', 'win').inc()
        metrics.trade_pnl.labels('test_s1').observe(100.0)
        metrics.strategy_equity.labels('test_s1', 'BTC/USDT').set(10500)

        from prometheus_client import generate_latest
        output = generate_latest(metrics.registry).decode()
        assert 'trades_total{result="win",side="buy",strategy_id="test_s1"} 1.0' in output
        assert 'strategy_equity{strategy_id="test_s1",symbol="BTC/USDT"} 10500' in output

    def test_path_normalization(self):
        from engine.metrics import metrics
        assert metrics._normalize_path('/api/strategies/abc123def') == '/api/strategies/:id'
        assert metrics._normalize_path('/api/sentiment/BTC_USDT') == '/api/sentiment/:symbol'
        assert metrics._normalize_path('/api/health') == '/api/health'

    def test_http_tracking(self):
        from engine.metrics import metrics
        # 9 字符字母数字混合 id
        metrics.track_http('GET', '/api/strategies/xyz789abc', 200, 0.05)
        from prometheus_client import generate_latest
        output = generate_latest(metrics.registry).decode()
        assert '/api/strategies/:id' in output


class TestAlerts:
    def test_dedup_within_cooldown(self):
        from engine.alerts import AlertManager, Alert, Severity

        class DummyCh:
            name = 'dummy'
            def __init__(self): self.received = []
            def send(self, a): self.received.append(a); return True

        mgr = AlertManager(dedup_cooldown_sec=300, min_severity=Severity.INFO)
        ch = DummyCh()
        mgr.channels.append(ch)

        for _ in range(5):
            mgr.send(Alert('Dup', 'same', Severity.WARNING, 'src', {'k': 'v'}))

        time.sleep(0.3)
        assert len(ch.received) == 1

    def test_different_fingerprints_both_sent(self):
        from engine.alerts import AlertManager, Alert, Severity

        class DummyCh:
            name = 'dummy'
            def __init__(self): self.received = []
            def send(self, a): self.received.append(a); return True

        mgr = AlertManager(dedup_cooldown_sec=300, min_severity=Severity.INFO)
        ch = DummyCh(); mgr.channels.append(ch)

        mgr.send(Alert('A', 'x', Severity.WARNING, 'src1'))
        mgr.send(Alert('A', 'x', Severity.WARNING, 'src2'))     # 不同 source → 不同指纹
        mgr.send(Alert('B', 'x', Severity.WARNING, 'src1'))     # 不同 title
        time.sleep(0.3)
        assert len(ch.received) == 3

    def test_severity_filter(self):
        from engine.alerts import AlertManager, Alert, Severity
        mgr = AlertManager(min_severity=Severity.WARNING)

        class DummyCh:
            name = 'dummy'
            def __init__(self): self.received = []
            def send(self, a): self.received.append(a); return True

        ch = DummyCh(); mgr.channels.append(ch)
        assert mgr.send(Alert('Low', 'x', Severity.INFO)) is False
        assert mgr.send(Alert('High', 'x', Severity.CRITICAL, 'src1')) is True
        time.sleep(0.3)
        assert len(ch.received) == 1

    def test_risk_event_mapping(self):
        from engine.alerts import risk_event_to_alert, Severity
        event = {
            'event_type': 'max_drawdown', 'level': 'critical',
            'strategy_id': 's1', 'symbol': 'BTC/USDT',
            'message': '回撤 25%', 'data': {'drawdown_pct': 25},
        }
        alert = risk_event_to_alert(event)
        assert alert.severity == Severity.CRITICAL
        assert '最大回撤' in alert.title
        assert alert.tags['strategy'] == 's1'
        assert alert.tags['drawdown_pct'] == '25'

    def test_telegram_channel_payload(self):
        from engine.alerts import TelegramChannel, Alert, Severity
        ch = TelegramChannel('fake_token', '12345')
        with patch('urllib.request.urlopen') as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            ok = ch.send(Alert('Test', 'body', Severity.WARNING, 'src'))
            assert ok is True
            call = mock_open.call_args[0][0]
            import json
            payload = json.loads(call.data.decode())
            assert payload['chat_id'] == '12345'
            assert 'Test' in payload['text']

    def test_webhook_feishu_format(self):
        from engine.alerts import WebhookChannel, Alert, Severity
        ch = WebhookChannel('http://fake', 'feishu')
        with patch('urllib.request.urlopen') as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            ch.send(Alert('T', 'b', Severity.WARNING, 's'))
            call = mock_open.call_args[0][0]
            import json
            payload = json.loads(call.data.decode())
            assert payload['msg_type'] == 'text'
            assert 'content' in payload

    def test_webhook_discord_format(self):
        from engine.alerts import WebhookChannel, Alert, Severity
        ch = WebhookChannel('http://fake', 'discord')
        with patch('urllib.request.urlopen') as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            ch.send(Alert('T', 'b', Severity.CRITICAL, 's', {'k': 'v'}))
            call = mock_open.call_args[0][0]
            import json
            payload = json.loads(call.data.decode())
            assert 'embeds' in payload
            assert payload['embeds'][0]['color'] == 0xe74c3c    # critical → red


class TestEnvLoading:
    def test_alerts_load_from_env(self, monkeypatch):
        from engine.alerts import AlertManager
        mgr = AlertManager()
        # 没设环境变量 → 0 渠道
        count = mgr.load_from_env()
        assert count == 0

        # 设 Telegram
        monkeypatch.setenv('ALERT_TELEGRAM_BOT_TOKEN', 'tk')
        monkeypatch.setenv('ALERT_TELEGRAM_CHAT_ID', '123')
        mgr2 = AlertManager()
        count = mgr2.load_from_env()
        assert count >= 1
        assert 'telegram' in [c.name for c in mgr2.channels]

        monkeypatch.setenv('ALERT_WEBHOOK_URL', 'http://fake')
        monkeypatch.setenv('ALERT_WEBHOOK_TYPE', 'feishu')
        mgr3 = AlertManager()
        count = mgr3.load_from_env()
        assert count >= 2
        names = [c.name for c in mgr3.channels]
        assert 'webhook' in names and 'telegram' in names
