
import sys, io
if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

#!/usr/bin/env python3
"""
apply_phase2.py — 应用 Phase 2 集成改动

将 SQLite 存储 / 对账器 / Kill Switch 接入 app.py。
失败的地方会打印提示,让你手动对照 app_integration_patch.md 修改。
"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
if not (ROOT / 'app.py').exists():
    print("[X] 请在项目根目录运行")
    sys.exit(1)


def backup(p: Path):
    bak = p.with_suffix(p.suffix + '.phase2.bak')
    if not bak.exists():
        shutil.copy2(p, bak)


def insert_after(path: Path, anchor: str, text: str, desc: str) -> bool:
    content = path.read_text(encoding='utf-8')
    if text.strip().split('\n')[0] in content:
        print(f"  [SKIP]  {desc}: 已存在")
        return True
    idx = content.find(anchor)
    if idx < 0:
        print(f"  [X] {desc}: 找不到 anchor")
        return False
    end = content.find('\n', idx) + 1
    new_content = content[:end] + text + content[end:]
    backup(path)
    path.write_text(new_content, encoding='utf-8')
    print(f"  [OK] {desc}")
    return True


def replace_in_file(path: Path, old: str, new: str, desc: str) -> bool:
    content = path.read_text(encoding='utf-8')
    if old not in content:
        if new.strip() in content:
            print(f"  [SKIP]  {desc}: 已应用")
            return True
        print(f"  [X] {desc}: 找不到原文")
        return False
    backup(path)
    path.write_text(content.replace(old, new), encoding='utf-8')
    print(f"  [OK] {desc}")
    return True


# ============================================================
# 1. 在 app.py 顶部追加 import
# ============================================================
print("\n[1/5] 向 app.py 注入新 import + 单例")

import_block = '''
# === Phase 2: 持久化 + 对账 + Kill Switch ===
from engine.storage import (
    db as _db,
    strategy_repo,
    exchange_repo,
    trade_repo,
    position_repo,
    risk_event_repo,
    audit_repo,
)
from engine.reconciliation import PositionReconciler
from engine.kill_switch import KillSwitch

reconciler = PositionReconciler(position_repo, risk_event_repo)
kill_switch = KillSwitch(audit_repo, risk_event_repo)
'''

insert_after(
    ROOT / 'app.py',
    "from engine.rl import rl_manager",
    import_block,
    "Phase 2 imports + 单例",
)

# ============================================================
# 2. 替换 load_strategies / save_strategies
# ============================================================
print("\n[2/5] 替换 JSON 读写函数为 SQLite 版本")

old_funcs = '''def load_strategies() -> dict:
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
    """加载原始(含加密标记)的交易所配置"""
    if EXCHANGE_FILE.exists():
        return json.loads(EXCHANGE_FILE.read_text())
    return {}'''

new_funcs = '''def load_strategies() -> dict:
    """从 SQLite 读所有策略 (Phase 2)"""
    return strategy_repo.list_all()

def save_strategies(data: dict):
    """向后兼容 — 批量覆盖。新代码请用 save_strategy(sid, config)"""
    for sid, cfg in data.items():
        strategy_repo.upsert(sid, cfg)

def save_strategy(sid: str, config: dict):
    """写单个策略"""
    strategy_repo.upsert(sid, config)

def load_exchange() -> dict:
    """解密后的 exchange 配置 (Phase 2)"""
    raw = exchange_repo.get()
    return decrypt_exchange_config(raw) if raw else {}

def load_exchange_raw() -> dict:
    return exchange_repo.get()'''

replace_in_file(ROOT / 'app.py', old_funcs, new_funcs, "JSON -> SQLite 读写")


# ============================================================
# 3. api_save_exchange 写入走 exchange_repo
# ============================================================
print("\n[3/5] api_save_exchange 走 exchange_repo")

replace_in_file(
    ROOT / 'app.py',
    "encrypted = encrypt_exchange_config(data)\n    EXCHANGE_FILE.write_text(json.dumps(encrypted, ensure_ascii=False, indent=2))",
    "encrypted = encrypt_exchange_config(data)\n    exchange_repo.save(encrypted)",
    "api_save_exchange",
)


# ============================================================
# 4. 注入新路由 (kill_switch / reconcile / trades / audit)
# ============================================================
print("\n[4/5] 追加 Phase 2 API 路由")

new_routes = '''

# ================================================================
# API: Kill Switch (紧急停止)  — Phase 2
# ================================================================

@app.route('/api/kill-switch/status', methods=['GET'])
def api_kill_switch_status():
    return jsonify(kill_switch.status())


@app.route('/api/kill-switch/activate', methods=['POST'])
def api_kill_switch_activate():
    data = request.json or {}
    reason = data.get('reason', 'Manual trigger via API')
    flat = bool(data.get('flat_positions', False))
    by = data.get('by', 'api')
    result = kill_switch.activate(
        reason=reason, triggered_by=by, live_trader=live_trader,
        flat_all_positions=flat, make_exchange_client=_make_exchange_client,
    )
    if result.get('ok'):
        for sid in load_strategies():
            strategy_repo.update_status(sid, 'stopped')
    return jsonify(result), (200 if result.get('ok') else 409)


@app.route('/api/kill-switch/deactivate', methods=['POST'])
def api_kill_switch_deactivate():
    data = request.json or {}
    result = kill_switch.deactivate(by=data.get('by', 'admin'))
    return jsonify(result)


# ================================================================
# API: 对账  — Phase 2
# ================================================================

@app.route('/api/reconcile/<sid>', methods=['POST'])
def api_reconcile(sid):
    strategies = load_strategies()
    if sid not in strategies:
        return jsonify({'error': 'Strategy not found'}), 404
    raw_config = filter_strategy_config(strategies[sid])
    raw_config = decrypt_strategy_secrets(raw_config)
    exchange_cfg = load_exchange()
    for k in ('exchange_id', 'api_key', 'api_secret', 'passphrase'):
        if not raw_config.get(k):
            raw_config[k] = exchange_cfg.get(k, '')
    config = StrategyConfig(**raw_config)
    try:
        client = ExchangeClient(
            config.exchange_id, config.api_key,
            config.api_secret, config.passphrase,
        )
        reports = reconciler.reconcile_strategy(config, client)
        return jsonify({
            'strategy_id': sid,
            'reports': [r.to_dict() for r in reports],
            'all_ok': all(r.status == 'ok' for r in reports),
        })
    except Exception as e:
        logger.error(f"Reconcile error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


# ================================================================
# API: 交易流水 (持久化)  — Phase 2
# ================================================================

@app.route('/api/trades/recent', methods=['GET'])
def api_trades_recent():
    limit = int(request.args.get('limit', 100))
    sid = request.args.get('strategy_id')
    trades = (trade_repo.recent_for_strategy(sid, limit) if sid
              else trade_repo.recent_all(limit))
    return jsonify({'count': len(trades), 'trades': trades})


@app.route('/api/trades/stats/<sid>', methods=['GET'])
def api_trade_stats(sid):
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

content = (ROOT / 'app.py').read_text(encoding='utf-8')
if "'/api/kill-switch/status'" in content:
    print("  [SKIP]  API 路由: 已存在")
else:
    # 插在 if __name__ == '__main__': 之前
    marker = "if __name__ == '__main__':"
    if marker in content:
        new_content = content.replace(marker, new_routes + '\n\n' + marker)
        backup(ROOT / 'app.py')
        (ROOT / 'app.py').write_text(new_content, encoding='utf-8')
        print("  [OK] 注入 7 个新 API 路由")
    else:
        print("  [X] 找不到 main 入口,请手动追加路由")


# ============================================================
# 5. 修复 Transformer UserWarning (顺手)
# ============================================================
print("\n[5/5] 修复 Transformer UserWarning")

replace_in_file(
    ROOT / 'engine' / 'models.py',
    "self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)",
    "self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers, enable_nested_tensor=False)",
    "Transformer UserWarning",
)


print("\n" + "=" * 60)
print("[OK] Phase 2 应用完成")
print("\n下一步 (必做):")
print("  1. 手动应用 app_integration_patch.md 步骤 6-7")
print("     (LiveTrader 的 trade/position 持久化)")
print("     (RiskManager._emit_event 落盘)")
print("  2. 运行测试: python -m pytest tests/ -v")
print("  3. 启动: python app.py")
print("  4. 测试新 API:")
print("     curl -u admin:$ADMIN_PASS http://localhost:8080/api/kill-switch/status")
print("     curl -u admin:$ADMIN_PASS http://localhost:8080/api/audit/recent")
print("=" * 60)
