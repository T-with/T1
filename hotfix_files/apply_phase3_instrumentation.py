"""
apply_phase3_instrumentation.py — 给 engine/core.py LiveTrader 埋指标 + 日志

在交易、订单、模型训练的关键节点加:
- metrics.trades_total, trade_pnl, orders_total 等 Counter 自增
- structlog 带 strategy_id/symbol 的日志

这一步是对 Phase 2 已经改好的 LiveTrader._close_position 的增强,
不会重复改(有 "metrics" 关键字就跳过)。
"""
import sys, io
if sys.platform == 'win32' and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import re, shutil, sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
CORE = ROOT / 'engine' / 'core.py'
if not CORE.exists():
    print("[X] 在项目根目录运行"); sys.exit(1)


def backup(p):
    b = p.with_suffix(p.suffix + '.phase3.bak')
    if not b.exists():
        shutil.copy2(p, b)


# ============================================================
# 1. _close_position — 写入 trade_repo 的地方追加 metrics
# ============================================================
print("\n[1/3] LiveTrader._close_position: metrics + trade log")

content = CORE.read_text(encoding='utf-8')

if 'metrics.trades_total' in content:
    print("  [SKIP]  已埋点")
else:
    # 找到 Phase 2 加的 trade_repo.insert 块,在它之后加指标
    old = """        # Phase 2: 持久化交易
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
            logger.warning(f"Failed to persist trade: {e}")"""

    new = """        # Phase 2: 持久化交易
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

        # Phase 3: Prometheus 指标 + 结构化日志
        try:
            from engine.metrics import metrics
            result = 'win' if pnl > 0 else ('loss' if pnl < 0 else 'break_even')
            metrics.trades_total.labels(config.id, pos['side'], result).inc()
            metrics.trade_pnl.labels(config.id).observe(pnl)
            metrics.trade_pnl_pct.labels(config.id).observe(pnl_pct)
        except Exception:
            pass
        try:
            import structlog
            structlog.get_logger('live_trader').info(
                'trade_closed',
                strategy_id=config.id, symbol=config.symbol,
                side=pos['side'], reason=reason,
                entry=round(pos.get('entry_price', 0), 4),
                exit=round(current_price, 4),
                pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 3),
                paper=config.paper,
            )
        except Exception:
            pass"""

    if old in content:
        backup(CORE)
        CORE.write_text(content.replace(old, new), encoding='utf-8')
        print("  [OK] 交易完成埋点")
    else:
        print("  [X] 找不到 Phase 2 trade_repo.insert 块 — 请先跑 apply_phase2.py")


# ============================================================
# 2. 开仓 — position_repo.upsert 附近
# ============================================================
print("\n[2/3] LiveTrader._run_loop: 开仓埋点")

content = CORE.read_text(encoding='utf-8')
if 'metrics.orders_total.labels' in content:
    print("  [SKIP]  已埋点")
else:
    # 开多分支 (long)
    old_long = """                            state['positions'][config.symbol] = {
                                'side': 'long',
                                'size': amount_usdt / current_price,
                                'entry_price': current_price,
                                'current_price': current_price,
                                'opened_at': datetime.now().isoformat(),
                            }
                            state['highest_price'] = current_price
                            state['lowest_price'] = current_price"""

    new_long = """                            state['positions'][config.symbol] = {
                                'side': 'long',
                                'size': amount_usdt / current_price,
                                'entry_price': current_price,
                                'current_price': current_price,
                                'opened_at': datetime.now().isoformat(),
                            }
                            state['highest_price'] = current_price
                            state['lowest_price'] = current_price
                            # Phase 3: 埋点
                            try:
                                from engine.metrics import metrics
                                metrics.orders_total.labels(
                                    config.exchange_id, 'buy', 'market',
                                    'paper' if config.paper else 'filled'
                                ).inc()
                                # 持久化仓位
                                from engine.storage import position_repo
                                position_repo.upsert(config.id, config.symbol,
                                                     state['positions'][config.symbol])
                            except Exception:
                                pass
                            try:
                                import structlog
                                structlog.get_logger('live_trader').info(
                                    'position_opened',
                                    strategy_id=config.id, symbol=config.symbol,
                                    side='long', size=state['positions'][config.symbol]['size'],
                                    entry=current_price, paper=config.paper,
                                )
                            except Exception:
                                pass"""

    if old_long in content:
        backup(CORE)
        content = content.replace(old_long, new_long)
        CORE.write_text(content, encoding='utf-8')
        print("  [OK] 开仓埋点")
    else:
        print("  [!]  找不到开多代码块,跳过 (可能代码已改,需手动)")


# ============================================================
# 3. BacktestEngine.run / 模型训练的耗时
# ============================================================
print("\n[3/3] 模型训练耗时埋点 (可选)")
# 这个留给 Phase 4 — 现在不改

print("\n" + "=" * 60)
print("[OK] LiveTrader 埋点完成")
print("检查: 运行策略后 curl /metrics | grep trades_total")
print("=" * 60)
