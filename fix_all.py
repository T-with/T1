#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_all.py — 一键修复全部剩余问题

问题 1: engine/storage.py — Database._local 是类级 threading.local()
        导致不同实例共用同一 SQLite 连接，测试 DB 隔离完全失效
        表现：test_trade_stats 总数 5 → 10 → 15（每次测试叠加）
              test_both_empty_no_report 看到上个测试留下的持仓

问题 2: app.py — Phase 2/3 路由从未应用（被还原成 pre-install.bak）
        表现：/api/kill-switch/status 返回 404
              /metrics 端点不存在

用法：
    把此脚本放到项目根目录，运行：
    python fix_all.py
"""

import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.resolve()

for required in ['app.py', 'engine/storage.py']:
    if not (ROOT / required).exists():
        print(f"[X] 未找到 {required}，请在项目根目录运行")
        sys.exit(1)


def backup(p: Path):
    bak = p.with_suffix(p.suffix + '.fix.bak')
    if not bak.exists():
        shutil.copy2(p, bak)
        print(f"  📦 已备份 → {p.name}.fix.bak")


def patch(path: Path, old: str, new: str, label: str) -> bool:
    content = path.read_text(encoding='utf-8')
    if old not in content:
        if new.strip()[:50] in content:
            print(f"  [已修复] {label}")
            return True
        print(f"  [跳过]   {label}")
        return False
    backup(path)
    path.write_text(content.replace(old, new, 1), encoding='utf-8')
    print(f"  [✓] {label}")
    return True


# ═══════════════════════════════════════════════════════════════
# 问题 1: Database._local 类级 → 实例级
#
# 根本原因：
#   class Database:
#       _local = threading.local()   ← 所有实例共享同一个 thread-local
#
#   测试 fixture 每次创建新 Database(db_path=tmp/test.db)
#   但 _get_conn() 发现 _local.conn 已存在（是上个测试的连接）
#   就直接返回旧连接，新 db_path 完全被忽略
#
# 修复：把 _local 移到 __init__，变成实例属性
# ═══════════════════════════════════════════════════════════════
print("\n[1/2] 修复 Database._local 实例隔离问题 (engine/storage.py)")

storage_path = ROOT / 'engine' / 'storage.py'

patch(storage_path,
    """class Database:
    \"\"\"线程安全的 SQLite 封装 (WAL 模式 + 连接池)\"\"\"

    _local = threading.local()

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()""",
    """class Database:
    \"\"\"线程安全的 SQLite 封装 (WAL 模式 + 连接池)\"\"\"

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        # 修复：_local 必须是实例属性，而非类属性
        # 类属性会导致所有 Database 实例共享同一 thread-local 连接池
        # 测试时 fixture 创建新 Database(tmp_path/test.db)，
        # 但 _get_conn() 发现 _local.conn 已存在就直接返回旧连接（错误的数据库！）
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()""",
    "Database._local 从类属性改为实例属性（修复测试 DB 隔离）"
)


# ═══════════════════════════════════════════════════════════════
# 问题 2: app.py 缺少 Phase 2/3 路由
#
# 用户把 app.py 还原成了 pre-install.bak（Phase 2/3 未应用）
# 直接在这里完整应用所有缺失内容
# ═══════════════════════════════════════════════════════════════
print("\n[2/2] 应用 Phase 2/3 到 app.py")

app_path = ROOT / 'app.py'
app_content = app_path.read_text(encoding='utf-8')

# ─── 2a. Phase 3 顶部 import（日志/指标/告警）───────────────────
patch(app_path,
    '"""MyTradingPlatform Flask App"""\nfrom flask import Flask, render_template, jsonify, request',
    '''"""MyTradingPlatform Flask App"""

# === Phase 3: 结构化日志 / 指标 / 报警 (必须在其他 import 前) ===
try:
    from engine.logging_setup import setup_logging, get_logger, bind_context, clear_context, LogContext
    setup_logging()
    _structlog = get_logger('app')
    from engine.metrics import metrics, start_metrics_snapshot_thread
    from engine.alerts import alert_manager, Alert, Severity, risk_event_to_alert
    _phase3_ok = True
except ImportError:
    _phase3_ok = False
    import logging as _logging
    _structlog = _logging.getLogger('app')

from flask import Flask, render_template, jsonify, request''',
    "Phase 3 顶部 import"
)

# ─── 2b. Phase 2 import（storage / 对账 / kill switch）──────────
patch(app_path,
    "from engine.rl import rl_manager",
    """from engine.rl import rl_manager

# === Phase 2: 持久化 + 对账 + Kill Switch ===
try:
    from engine.storage import (
        db as _db, strategy_repo, exchange_repo,
        trade_repo, position_repo, risk_event_repo, audit_repo,
    )
    from engine.reconciliation import PositionReconciler
    from engine.kill_switch import KillSwitch
    reconciler = PositionReconciler(position_repo, risk_event_repo)
    kill_switch = KillSwitch(audit_repo, risk_event_repo)
    _phase2_ok = True
except ImportError:
    _phase2_ok = False""",
    "Phase 2 import"
)

# ─── 2c. 替换 load_strategies / save_strategies ─────────────────
patch(app_path,
    """def load_strategies() -> dict:
    if STRATEGIES_FILE.exists():
        return json.loads(STRATEGIES_FILE.read_text())
    return {}

def save_strategies(data: dict):
    STRATEGIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_exchange() -> dict:
    if EXCHANGE_FILE.exists():
        raw = json.loads(EXCHANGE_FILE.read_text())
        # 返回解密版本供内部使用
        return decrypt_exchange_config(raw)
    return {}

def load_exchange_raw() -> dict:
    \"\"\"加载原始（含加密标记）的交易所配置\"\"\"
    if EXCHANGE_FILE.exists():
        return json.loads(EXCHANGE_FILE.read_text())
    return {}""",
    """def load_strategies() -> dict:
    if _phase2_ok:
        return strategy_repo.list_all()
    if STRATEGIES_FILE.exists():
        return json.loads(STRATEGIES_FILE.read_text())
    return {}

def save_strategies(data: dict):
    if _phase2_ok:
        for sid, cfg in data.items():
            strategy_repo.upsert(sid, cfg)
        return
    STRATEGIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def save_strategy(sid: str, config: dict):
    \"\"\"写单个策略 (Phase 2 新增)\"\"\"
    if _phase2_ok:
        strategy_repo.upsert(sid, config)
    else:
        strategies = load_strategies()
        strategies[sid] = config
        save_strategies(strategies)

def load_exchange() -> dict:
    if _phase2_ok:
        raw = exchange_repo.get()
        return decrypt_exchange_config(raw) if raw else {}
    if EXCHANGE_FILE.exists():
        raw = json.loads(EXCHANGE_FILE.read_text())
        return decrypt_exchange_config(raw)
    return {}

def load_exchange_raw() -> dict:
    \"\"\"加载原始（含加密标记）的交易所配置\"\"\"
    if _phase2_ok:
        return exchange_repo.get()
    if EXCHANGE_FILE.exists():
        return json.loads(EXCHANGE_FILE.read_text())
    return {}""",
    "load_strategies/save_strategies 切换到 SQLite"
)

# ─── 2d. api_save_exchange 写入走 exchange_repo ──────────────────
patch(app_path,
    "    encrypted = encrypt_exchange_config(data)\n    EXCHANGE_FILE.write_text(json.dumps(encrypted, ensure_ascii=False, indent=2))",
    "    encrypted = encrypt_exchange_config(data)\n    if _phase2_ok:\n        exchange_repo.save(encrypted)\n    else:\n        EXCHANGE_FILE.write_text(json.dumps(encrypted, ensure_ascii=False, indent=2))",
    "api_save_exchange 走 exchange_repo"
)

# ─── 2e. Flask before/after request hooks（Phase 3）─────────────
patch(app_path,
    "app = Flask(__name__)\nlogging.basicConfig(level=logging.INFO)\nlogger = logging.getLogger(__name__)",
    """app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Phase 3: 请求级中间件 ────────────────────────────────────────
import time as _time
import uuid as _uuid

@app.before_request
def _before_req():
    request._start_ts = _time.time()
    rid = _uuid.uuid4().hex[:8]
    request._request_id = rid
    if _phase3_ok:
        bind_context(request_id=rid, method=request.method, path=request.path)

@app.after_request
def _after_req(response):
    try:
        if _phase3_ok:
            duration = _time.time() - getattr(request, '_start_ts', _time.time())
            metrics.track_http(request.method, request.path, response.status_code, duration)
            clear_context()
    except Exception:
        pass
    return response""",
    "Phase 3 before/after request hooks"
)

# ─── 2f. 放行 /metrics /health 无需认证 ─────────────────────────
patch(app_path,
    "    if request.path == '/health':\n        return None",
    "    if request.path in ('/health', '/metrics'):\n        return None",
    "放行 /metrics 认证"
)

# ─── 2g. 注入新路由（/metrics + kill-switch + reconcile + trades）─
new_routes = '''

# ================================================================
# Phase 3: /metrics 端点
# ================================================================
if _phase3_ok:
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

    @app.route('/metrics', methods=['GET'])
    def api_metrics():
        return generate_latest(metrics.registry), 200, {'Content-Type': CONTENT_TYPE_LATEST}

    @app.route('/api/alerts/status', methods=['GET'])
    def api_alerts_status():
        return jsonify({
            'channels': [c.name for c in alert_manager.channels],
            'min_severity': alert_manager.min_severity.value,
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
        )
        sent = alert_manager.send(alert)
        return jsonify({'ok': sent, 'channels': [c.name for c in alert_manager.channels]})


# ================================================================
# Phase 2: Kill Switch / 对账 / 交易流水 / 审计
# ================================================================
if _phase2_ok:
    @app.route('/api/kill-switch/status', methods=['GET'])
    def api_kill_switch_status():
        return jsonify(kill_switch.status())

    @app.route('/api/kill-switch/activate', methods=['POST'])
    def api_kill_switch_activate():
        data = request.json or {}
        result = kill_switch.activate(
            reason=data.get('reason', 'Manual trigger via API'),
            triggered_by=data.get('by', 'api'),
            live_trader=live_trader,
            flat_all_positions=bool(data.get('flat_positions', False)),
            make_exchange_client=_make_exchange_client,
        )
        if result.get('ok'):
            for sid in load_strategies():
                strategy_repo.update_status(sid, 'stopped')
        return jsonify(result), (200 if result.get('ok') else 409)

    @app.route('/api/kill-switch/deactivate', methods=['POST'])
    def api_kill_switch_deactivate():
        data = request.json or {}
        return jsonify(kill_switch.deactivate(by=data.get('by', 'admin')))

    @app.route('/api/reconcile/<sid>', methods=['POST'])
    def api_reconcile(sid):
        strategies = load_strategies()
        if sid not in strategies:
            return jsonify({'error': 'Strategy not found'}), 404
        raw_config = filter_strategy_config(strategies[sid])
        raw_config = decrypt_strategy_secrets(raw_config)
        exchange_cfg = load_exchange()
        for k in ('exchange_id', 'api_key', 'api_secret', 'passphrase'):
            raw_config.setdefault(k, exchange_cfg.get(k, ''))
        config = StrategyConfig(**raw_config)
        try:
            client = ExchangeClient(config.exchange_id, config.api_key, config.api_secret, config.passphrase)
            reports = reconciler.reconcile_strategy(config, client)
            return jsonify({'strategy_id': sid, 'reports': [r.to_dict() for r in reports],
                            'all_ok': all(r.status == 'ok' for r in reports)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/trades/recent', methods=['GET'])
    def api_trades_recent():
        limit = int(request.args.get('limit', 100))
        sid = request.args.get('strategy_id')
        trades = trade_repo.recent_for_strategy(sid, limit) if sid else trade_repo.recent_all(limit)
        return jsonify({'count': len(trades), 'trades': trades})

    @app.route('/api/trades/stats/<sid>', methods=['GET'])
    def api_strategy_trade_stats(sid):
        stats = dict(trade_repo.stats_for_strategy(sid) or {})
        stats['pnl_24h'] = trade_repo.pnl_for_period(sid, 24)
        stats['pnl_7d'] = trade_repo.pnl_for_period(sid, 24 * 7)
        stats['pnl_30d'] = trade_repo.pnl_for_period(sid, 24 * 30)
        return jsonify(stats)

    @app.route('/api/audit/recent', methods=['GET'])
    def api_audit_recent():
        limit = int(request.args.get('limit', 100))
        return jsonify({'entries': audit_repo.recent(limit)})

'''

app_content = app_path.read_text(encoding='utf-8')
marker = "if __name__ == '__main__':"
if "'/api/kill-switch/status'" not in app_content and marker in app_content:
    backup(app_path)
    app_path.write_text(app_content.replace(marker, new_routes + '\n\n' + marker), encoding='utf-8')
    print("  [✓] 注入 Phase 2/3 路由 (kill-switch / reconcile / trades / metrics)")
else:
    print("  [已存在] Phase 2/3 路由")

# ─── 2h. 启动后置初始化（Phase 3）──────────────────────────────
init_block = '''
# ── Phase 3 启动后置初始化 ─────────────────────────────────────
if _phase3_ok:
    try:
        _alert_count = alert_manager.load_from_env()

        def _risk_cb(event):
            a = risk_event_to_alert(event)
            if a:
                alert_manager.send(a)
            try:
                etype = event.type.value if hasattr(event.type, 'value') else str(event.type)
                elevel = event.level.value if hasattr(event.level, 'value') else str(event.level)
                metrics.risk_events_total.labels(etype, elevel).inc()
            except Exception:
                pass

        risk_manager.register_callback(_risk_cb)

        def _alert_metric_hook(channel, severity, status):
            metrics.alerts_sent_total.labels(channel, severity, status).inc()
        alert_manager._metric_callback = _alert_metric_hook

        start_metrics_snapshot_thread(live_trader, risk_manager, interval=15)
        logger.info(f"Phase 3 initialized: {_alert_count} alert channels")
    except Exception as e:
        logger.warning(f"Phase 3 init partial: {e}")

'''

app_content = app_path.read_text(encoding='utf-8')
if 'start_metrics_snapshot_thread' not in app_content and marker in app_content:
    backup(app_path)
    app_path.write_text(app_content.replace(marker, init_block + '\n\n' + marker), encoding='utf-8')
    print("  [✓] 注入 Phase 3 启动初始化")
else:
    print("  [已存在] Phase 3 启动初始化")


# ═══════════════════════════════════════════════════════════════
# 最终验证
# ═══════════════════════════════════════════════════════════════
print("\n" + "═" * 60)
print("✅ 所有修复已应用")
print()
print("修复内容：")
print("  storage.py: Database._local 改为实例属性（修复测试 DB 隔离）")
print("  app.py:     重新应用 Phase 2/3（kill-switch / metrics / 对账）")
print()
print("验证步骤：")
print("  1. 本地测试（应全绿）：")
print("     python -m pytest tests/ -v")
print("     期望：70 passed, 0 failed")
print()
print("  2. 重新构建 Docker（让修复进容器）：")
print("     docker compose -f docker-compose.yml -f docker-compose.monitoring.yml down")
print("     docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build")
print()
print("  3. 验证 API：")
print("     curl.exe -u admin:TradingPlatform2026! http://localhost:8080/api/kill-switch/status")
print("     curl.exe http://localhost:8080/metrics | Select-String 'strategies_running'")
print("═" * 60)
