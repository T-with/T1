# Patch: app.py Phase 2 集成

下面这批改动把 SQLite 存储层、持仓对账、紧急停止 接入到现有 Flask app。
可以一步步应用,每步都能独立跑起来。

---

## 步骤 1: 新增 import + 实例化

**位置**: `app.py` 顶部 import 区域,紧跟原来 engine import 之后

```python
# === Phase 2 additions ===
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

# 单例
reconciler = PositionReconciler(position_repo, risk_event_repo)
kill_switch = KillSwitch(audit_repo, risk_event_repo)
```

---

## 步骤 2: 替换旧的 JSON 读写函数

**位置**: `app.py` 中 `load_strategies()` / `save_strategies()` / `load_exchange*()`

**旧代码删除**:
```python
STRATEGIES_FILE = DATA_DIR / 'strategies.json'
EXCHANGE_FILE = DATA_DIR / 'exchange.json'

def load_strategies() -> dict: ...
def save_strategies(data: dict): ...
def load_exchange() -> dict: ...
def load_exchange_raw() -> dict: ...
```

**换成**:
```python
def load_strategies() -> dict:
    """从 SQLite 读取所有策略 (向后兼容接口)"""
    return strategy_repo.list_all()

def save_strategy(sid: str, config: dict):
    """写单个策略 (旧代码用 save_strategies(整体字典),这里拆细)"""
    strategy_repo.upsert(sid, config)

def load_exchange() -> dict:
    """解密后的 exchange 配置"""
    raw = exchange_repo.get()
    return decrypt_exchange_config(raw) if raw else {}

def load_exchange_raw() -> dict:
    return exchange_repo.get()
```

**然后把代码里所有调用 `save_strategies(strategies)` 的地方改成**:
```python
# 旧: save_strategies(strategies)
# 新: 只写改动的那一条
save_strategy(sid, strategies[sid])
```

具体需要改的地方:
- `api_create_strategy` → `save_strategy(sid, config.__dict__)`
- `api_delete_strategy` → `strategy_repo.delete(sid)`
- `api_live_start` → `save_strategy(sid, config.__dict__)`
- `api_live_stop` → `strategy_repo.update_status(sid, 'stopped')`

---

## 步骤 3: 交易所配置接口切换

**`api_save_exchange` 内替换**:
```python
# 旧: EXCHANGE_FILE.write_text(json.dumps(encrypted, ...))
# 新:
exchange_repo.save(encrypted)
```

---

## 步骤 4: 启动策略时调用对账器

**位置**: `api_live_start`

```python
@app.route('/api/live/start/<sid>', methods=['POST'])
def api_live_start(sid):
    # 紧急停止激活时禁止启动
    if kill_switch.active:
        return jsonify({
            'error': 'Kill switch is active',
            'status': kill_switch.status(),
        }), 403

    strategies = load_strategies()
    if sid not in strategies:
        return jsonify({'error': 'Strategy not found'}), 404

    raw_config = filter_strategy_config(strategies[sid])
    raw_config = decrypt_strategy_secrets(raw_config)

    exchange_cfg = load_exchange()
    raw_config['exchange_id'] = exchange_cfg.get('exchange_id', raw_config.get('exchange_id', 'binance'))
    raw_config['api_key'] = exchange_cfg.get('api_key', raw_config.get('api_key', ''))
    raw_config['api_secret'] = exchange_cfg.get('api_secret', raw_config.get('api_secret', ''))
    raw_config['passphrase'] = exchange_cfg.get('passphrase', raw_config.get('passphrase', ''))

    config = StrategyConfig(**raw_config)

    # ============================================================
    # Phase 2: 启动前持仓对账
    # ============================================================
    if not config.paper and config.api_key:
        try:
            client = ExchangeClient(
                config.exchange_id, config.api_key,
                config.api_secret, config.passphrase,
            )
            reports = reconciler.reconcile_strategy(config, client)
            halt_reasons = [r for r in reports if r.action_recommended == 'halt']
            if halt_reasons:
                return jsonify({
                    'error': 'Position reconciliation failed - manual review required',
                    'reports': [r.to_dict() for r in reports],
                }), 409
            # 从 DB 恢复内存持仓
            restored = reconciler.restore_from_db(sid)
            if restored:
                logger.info(f"Restored {len(restored)} positions for {sid}")
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            # 不阻止启动,但记录警告

    if live_trader.start(config):
        save_strategy(sid, config.__dict__)
        audit_repo.log('start_strategy', sid, 'admin',
                       {'paper': config.paper, 'symbol': config.symbol})
        logger.info(f"Strategy started: {sid} ({config.name})")
        return jsonify({'ok': True, 'message': f'Strategy {config.name} started'})
    return jsonify({'error': 'Already running'}), 400
```

---

## 步骤 5: 新增紧急停止 API

```python
# ================================================================
# API: Kill Switch (紧急停止)
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
        reason=reason,
        triggered_by=by,
        live_trader=live_trader,
        flat_all_positions=flat,
        make_exchange_client=_make_exchange_client,
    )

    # 把所有 DB 里的策略状态也标记为 stopped
    if result.get('ok'):
        for sid in load_strategies():
            strategy_repo.update_status(sid, 'stopped')

    return jsonify(result), 200 if result['ok'] else 409


@app.route('/api/kill-switch/deactivate', methods=['POST'])
def api_kill_switch_deactivate():
    data = request.json or {}
    by = data.get('by', 'admin')
    result = kill_switch.deactivate(by=by)
    return jsonify(result)


# ================================================================
# API: 对账
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
# API: 历史交易 (持久化后可用)
# ================================================================

@app.route('/api/trades/recent', methods=['GET'])
def api_trades_recent():
    limit = int(request.args.get('limit', 100))
    sid = request.args.get('strategy_id')
    if sid:
        trades = trade_repo.recent_for_strategy(sid, limit)
    else:
        trades = trade_repo.recent_all(limit)
    return jsonify({'count': len(trades), 'trades': trades})


@app.route('/api/trades/stats/<sid>', methods=['GET'])
def api_trade_stats(sid):
    stats = trade_repo.stats_for_strategy(sid)
    stats['pnl_24h'] = trade_repo.pnl_for_period(sid, 24)
    stats['pnl_7d'] = trade_repo.pnl_for_period(sid, 24 * 7)
    stats['pnl_30d'] = trade_repo.pnl_for_period(sid, 24 * 30)
    return jsonify(stats)


# ================================================================
# API: 审计日志
# ================================================================

@app.route('/api/audit/recent', methods=['GET'])
def api_audit_recent():
    limit = int(request.args.get('limit', 100))
    return jsonify({'entries': audit_repo.recent(limit)})
```

---

## 步骤 6: LiveTrader 内部写入 trade/position 到 DB

**位置**: `engine/core.py` `LiveTrader._close_position`

在现有逻辑的 `state['trades'].append(trade_record)` 之后,追加:

```python
        # Phase 2: 持久化交易
        try:
            from engine.storage import trade_repo, position_repo
            trade_repo.insert({
                **trade_record,
                'strategy_id': config.id,
                'symbol': config.symbol,
                'paper': config.paper,
            })
            position_repo.delete(config.id, config.symbol)
        except Exception as e:
            logger.warning(f"Failed to persist trade: {e}")
```

**位置**: `LiveTrader._run_loop` 开仓后,在 `state['positions'][config.symbol] = {...}` 之后追加:

```python
        # Phase 2: 持久化开仓
        try:
            from engine.storage import position_repo
            position_repo.upsert(config.id, config.symbol, state['positions'][config.symbol])
        except Exception as e:
            logger.warning(f"Failed to persist position: {e}")
```

---

## 步骤 7: RiskManager 事件同步写 DB

**位置**: `engine/risk.py` `RiskManager._emit_event`

```python
    def _emit_event(self, event: RiskEvent):
        event.timestamp = time.time()
        with self._lock:
            self._risk_events.append(event)

        # Phase 2: 持久化
        try:
            from engine.storage import risk_event_repo
            risk_event_repo.log({
                'timestamp': event.timestamp,
                'event_type': event.type.value,
                'level': event.level.value,
                'strategy_id': event.strategy_id,
                'symbol': event.symbol,
                'message': event.message,
                'data': event.data,
                'action_taken': event.action_taken,
            })
        except Exception as e:
            logger.debug(f"Failed to persist risk event: {e}")

        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Risk callback error: {e}")
        logger.warning(f"RISK EVENT [{event.level.value}] {event.type.value}: {event.message}")
```
