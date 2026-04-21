"""
phase3_app_patch.py — 应用 Phase 3 到 app.py

自动插入:
1. 顶部 init: 日志 / 指标 / 报警
2. Flask before/after request hooks: 请求 ID、指标、日志上下文
3. /metrics 端点
4. /api/alerts/test 和 /api/alerts/status 路由
5. RiskManager 注册回调 -> 自动发告警
6. 定期指标快照线程
"""
import sys, io
if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
APP = ROOT / 'app.py'
if not APP.exists():
    print("[X] 在项目根目录运行")
    sys.exit(1)


def backup(p):
    bak = p.with_suffix(p.suffix + '.phase3.bak')
    if not bak.exists():
        shutil.copy2(p, bak)


def replace(path, old, new, desc):
    c = path.read_text(encoding='utf-8')
    if old not in c:
        if new.strip()[:40] in c:
            print(f"  [SKIP]  {desc}: 已应用")
            return True
        print(f"  [X] {desc}: 找不到原文 — 请手动修改")
        return False
    backup(path)
    path.write_text(c.replace(old, new), encoding='utf-8')
    print(f"  [OK] {desc}")
    return True


def insert_after_anchor(path, anchor, block, desc):
    c = path.read_text(encoding='utf-8')
    first_line = block.strip().split('\n')[0]
    if first_line and first_line in c:
        print(f"  [SKIP]  {desc}: 已存在")
        return True
    idx = c.find(anchor)
    if idx < 0:
        print(f"  [X] {desc}: 找不到 anchor")
        return False
    end = c.find('\n', idx) + 1
    backup(path)
    path.write_text(c[:end] + block + c[end:], encoding='utf-8')
    print(f"  [OK] {desc}")
    return True


# ============================================================
# 1. 顶部注入 import + 初始化
# ============================================================
print("\n[1/5] 顶部注入日志/指标/报警初始化")

# 必须在 import Flask 之前 setup_logging,才能接管所有后续 logger
top_block = '''
# === Phase 3: 结构化日志 / 指标 / 报警 (必须在其他 import 前) ===
from engine.logging_setup import setup_logging, get_logger, bind_context, clear_context, LogContext
setup_logging()
_structlog = get_logger('app')

from engine.metrics import metrics, start_metrics_snapshot_thread
from engine.alerts import alert_manager, Alert, Severity, risk_event_to_alert
'''

# 插在 from flask import ... 之前
replace(
    APP,
    '"""MyTradingPlatform Flask App"""\nfrom flask import Flask, render_template, jsonify, request',
    '"""MyTradingPlatform Flask App"""\n' + top_block + '\nfrom flask import Flask, render_template, jsonify, request',
    "顶部 import + setup_logging",
)


# ============================================================
# 2. Flask before/after request — 请求上下文
# ============================================================
print("\n[2/5] 请求级日志上下文 + 指标采集")

req_hooks = '''

# ================================================================
# Phase 3: 请求级中间件 — 日志上下文 + 指标
# ================================================================
import time as _time
import uuid as _uuid

@app.before_request
def _before_req():
    request._start_ts = _time.time()
    rid = _uuid.uuid4().hex[:8]
    request._request_id = rid
    bind_context(
        request_id=rid,
        method=request.method,
        path=request.path,
    )

@app.after_request
def _after_req(response):
    try:
        duration = _time.time() - getattr(request, '_start_ts', _time.time())
        metrics.track_http(request.method, request.path, response.status_code, duration)

        # 仅 4xx/5xx 写 warn 级日志
        if response.status_code >= 500:
            _structlog.error('http_request', status=response.status_code,
                             duration_ms=round(duration*1000, 1))
        elif response.status_code >= 400:
            _structlog.warning('http_request', status=response.status_code,
                               duration_ms=round(duration*1000, 1))
        else:
            _structlog.info('http_request', status=response.status_code,
                            duration_ms=round(duration*1000, 1))
    except Exception:
        pass
    clear_context()
    return response
'''

insert_after_anchor(
    APP,
    "app = Flask(__name__)",
    req_hooks,
    "before/after request hooks",
)


# ============================================================
# 3. 新路由 — /metrics, /api/alerts/*
# ============================================================
print("\n[3/5] 追加 /metrics + 告警 API")

new_routes = '''

# ================================================================
# Phase 3: /metrics + 告警测试 API
# ================================================================
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

@app.route('/metrics', methods=['GET'])
def api_metrics():
    """Prometheus 抓取端点 — 无需 Basic Auth 才能被抓取"""
    return generate_latest(metrics.registry), 200, {'Content-Type': CONTENT_TYPE_LATEST}


@app.route('/api/alerts/status', methods=['GET'])
def api_alerts_status():
    return jsonify({
        'channels': [c.name for c in alert_manager.channels],
        'min_severity': alert_manager.min_severity.value,
        'dedup_cooldown_sec': alert_manager.dedup_cooldown_sec,
        'queue_size': alert_manager._queue.qsize(),
    })


@app.route('/api/alerts/test', methods=['POST'])
def api_alerts_test():
    data = request.json or {}
    alert = Alert(
        title=data.get('title', 'Test Alert'),
        message=data.get('message', 'This is a test alert'),
        severity=Severity(data.get('severity', 'warning')),
        source='manual_test',
        tags=data.get('tags', {}),
    )
    sent = alert_manager.send(alert)
    return jsonify({'ok': sent,
                    'channels': [c.name for c in alert_manager.channels]})
'''

content = APP.read_text(encoding='utf-8')
if "'/metrics'" in content and "@app.route('/metrics'" in content:
    print("  [SKIP]  /metrics 已存在")
else:
    marker = "if __name__ == '__main__':"
    if marker in content:
        backup(APP)
        APP.write_text(content.replace(marker, new_routes + '\n\n' + marker),
                       encoding='utf-8')
        print("  [OK] 注入 3 个新路由 (/metrics, alerts/status, alerts/test)")


# ============================================================
# 4. 放行 /metrics 的 Basic Auth
# ============================================================
print("\n[4/5] /metrics 不经过 Basic Auth")

replace(
    APP,
    '''    if request.path == '/health':
        return None''',
    '''    if request.path in ('/health', '/metrics'):
        return None''',
    "放行 /metrics 认证",
)


# ============================================================
# 5. 启动后置初始化 — 加载告警 + 注册风控回调 + 起快照线程
# ============================================================
print("\n[5/5] 启动后置初始化")

init_block = '''

# ================================================================
# Phase 3: 启动时的后置初始化
# ================================================================

# 5.1 加载告警渠道
_alert_count = alert_manager.load_from_env()
_structlog.info('alerts_loaded', channels=_alert_count)

# 5.2 注册 RiskManager 回调 — 每个风控事件自动发告警
def _risk_event_to_alert(event):
    a = risk_event_to_alert(event)
    if a is not None:
        alert_manager.send(a)
    # 同时更新 Prometheus 计数
    try:
        etype = event.type.value if hasattr(event.type, 'value') else str(event.type)
        elevel = event.level.value if hasattr(event.level, 'value') else str(event.level)
        metrics.risk_events_total.labels(etype, elevel).inc()
    except Exception:
        pass

risk_manager.register_callback(_risk_event_to_alert)

# 5.3 告警管理器把发送结果回传给 Prometheus
def _alert_metric_hook(channel, severity, status):
    metrics.alerts_sent_total.labels(channel, severity, status).inc()
alert_manager._metric_callback = _alert_metric_hook

# 5.4 启动定期指标快照线程 (15s 一次)
start_metrics_snapshot_thread(live_trader, risk_manager, interval=15)

# 5.5 应用信息
import platform
metrics.app_info.info({
    'version': '1.2-phase3',
    'python': platform.python_version(),
    'env': __import__('os').environ.get('ENV', 'dev'),
})

_structlog.info('app_ready',
                channels=[c.name for c in alert_manager.channels],
                metrics_snapshot_interval=15)
'''

content = APP.read_text(encoding='utf-8')
if 'start_metrics_snapshot_thread(live_trader' in content:
    print("  [SKIP]  启动初始化已存在")
else:
    # 插在 if __name__ == '__main__': 之前
    marker = "if __name__ == '__main__':"
    if marker in content:
        backup(APP)
        APP.write_text(content.replace(marker, init_block + '\n\n' + marker),
                       encoding='utf-8')
        print("  [OK] 启动初始化已插入")


print("\n" + "=" * 60)
print("[OK] Phase 3 应用完成")
print()
print("必做检查:")
print("  1. pip install -r requirements.txt   # structlog + prometheus-client")
print("  2. 在 .env 追加告警配置 (详见 phase3/alerts.env.example)")
print("  3. docker compose build --no-cache && docker compose up -d")
print("  4. 验证:")
print("     curl http://localhost:8080/metrics | head -30")
print("     curl -u admin:PASS http://localhost:8080/api/alerts/status")
print("     curl -u admin:PASS -X POST http://localhost:8080/api/alerts/test \\")
print("          -H 'Content-Type: application/json' \\")
print("          -d '{\"title\":\"测试\",\"severity\":\"critical\"}'")
print("  5. (可选) docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d")
print("           访问 http://localhost:3000 (admin/admin)")
print("=" * 60)
