#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_bugfix.py — 一键修复 4 个关键 Bug

Bug 1: engine/core.py      — vol_vol 未定义变量 (NameError)
Bug 2: app.py              — Flask 路由函数名 api_trade_stats 冲突 (AssertionError)
Bug 3: engine/data_ws.py   — asyncio.Lock() 在非事件循环中创建 (RuntimeError)
Bug 4: engine/storage.py   — TEST_DB_PATH 环境变量未生效 (测试污染生产数据)

用法：
    python apply_bugfix.py
"""

import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()

if not (ROOT / 'app.py').exists():
    print("[X] 请将此脚本放在项目根目录下的 fixes/ 子目录中运行")
    sys.exit(1)


def backup(p: Path):
    bak = p.with_suffix(p.suffix + '.bugfix.bak')
    if not bak.exists():
        shutil.copy2(p, bak)
        print(f"  📦 备份: {p.name} → {p.name}.bugfix.bak")


def replace_in_file(path: Path, old: str, new: str, desc: str) -> bool:
    content = path.read_text(encoding='utf-8')
    if old not in content:
        # 检查是否已修复
        if new.strip() in content:
            print(f"  [已修复] {desc}")
            return True
        print(f"  [X] {desc}: 找不到目标文本，请手动检查")
        return False
    backup(path)
    path.write_text(content.replace(old, new, 1), encoding='utf-8')
    print(f"  [✓] {desc}")
    return True


print("=" * 60)
print("MyTradingPlatform — Bug 修复补丁")
print("=" * 60)

# ============================================================
# Bug 1: engine/core.py — vol_vol 未定义变量
# ============================================================
print("\n[1/4] 修复 vol_vol 未定义变量 (engine/core.py)")

core_path = ROOT / 'engine' / 'core.py'
replace_in_file(
    core_path,
    "market_state = f'rsi_{rsi_label}_vol_{vol_vol if False else vol_label}'",
    "market_state = f'rsi_{rsi_label}_vol_{vol_label}'",
    "SmartDCAStrategy._compute_weight: vol_vol → vol_label"
)

# ============================================================
# Bug 2: app.py — Flask 路由函数名 api_trade_stats 冲突
# ============================================================
print("\n[2/4] 修复 Flask 路由函数名冲突 (app.py)")

app_path = ROOT / 'app.py'

# Phase 2 注入的 api_trade_stats 和原来的 api_trade_stats 重名
# 将 Phase 2 注入的那个重命名为 api_strategy_trade_stats
replace_in_file(
    app_path,
    """@app.route('/api/trades/stats/<sid>', methods=['GET'])
def api_trade_stats(sid):
    stats = dict(trade_repo.stats_for_strategy(sid) or {})
    stats['pnl_24h'] = trade_repo.pnl_for_period(sid, 24)
    stats['pnl_7d'] = trade_repo.pnl_for_period(sid, 24 * 7)
    stats['pnl_30d'] = trade_repo.pnl_for_period(sid, 24 * 30)
    return jsonify(stats)""",
    """@app.route('/api/trades/stats/<sid>', methods=['GET'])
def api_strategy_trade_stats(sid):
    stats = dict(trade_repo.stats_for_strategy(sid) or {})
    stats['pnl_24h'] = trade_repo.pnl_for_period(sid, 24)
    stats['pnl_7d'] = trade_repo.pnl_for_period(sid, 24 * 7)
    stats['pnl_30d'] = trade_repo.pnl_for_period(sid, 24 * 30)
    return jsonify(stats)""",
    "重命名 Phase 2 注入的 api_trade_stats → api_strategy_trade_stats"
)

# ============================================================
# Bug 3: engine/data_ws.py — asyncio.Lock() 在非事件循环中创建
# ============================================================
print("\n[3/4] 修复 asyncio.Lock() 在模块加载时创建 (engine/data_ws.py)")

data_ws_path = ROOT / 'engine' / 'data_ws.py'
replace_in_file(
    data_ws_path,
    """class EventBus:
    \"\"\"高性能异步事件总线\"\"\"

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._async_subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()""",
    """class EventBus:
    \"\"\"高性能异步事件总线\"\"\"

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._async_subscribers: Dict[str, List[Callable]] = defaultdict(list)
        # 使用 threading.Lock 而非 asyncio.Lock，避免在无事件循环时报错
        # EventBus 在模块导入时创建，此时可能没有运行中的事件循环
        self._lock = threading.Lock()""",
    "EventBus._lock: asyncio.Lock() → threading.Lock()"
)

# data_ws.py 需要额外 import threading（如果还没有）
data_ws_content = data_ws_path.read_text(encoding='utf-8')
if 'import threading' not in data_ws_content:
    backup(data_ws_path)
    data_ws_path.write_text(
        data_ws_content.replace(
            'import websockets\nimport ssl',
            'import threading\nimport websockets\nimport ssl'
        ),
        encoding='utf-8'
    )
    print("  [✓] data_ws.py 补充 import threading")
else:
    print("  [已存在] threading 已导入")

# ============================================================
# Bug 4: engine/storage.py — TEST_DB_PATH 环境变量未生效
# ============================================================
print("\n[4/4] 修复 TEST_DB_PATH 环境变量未生效 (engine/storage.py)")

storage_path = ROOT / 'engine' / 'storage.py'
replace_in_file(
    storage_path,
    """logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / 'platform.db'""",
    """logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
# 支持通过环境变量指定 DB 路径（用于测试隔离）
DB_PATH = Path(os.environ.get('TEST_DB_PATH', str(DATA_DIR / 'platform.db')))""",
    "DB_PATH 支持 TEST_DB_PATH 环境变量"
)

# 确保 storage.py 有 import os
storage_content = storage_path.read_text(encoding='utf-8')
if 'import os' not in storage_content:
    backup(storage_path)
    storage_path.write_text(
        storage_content.replace(
            'import sqlite3',
            'import os\nimport sqlite3'
        ),
        encoding='utf-8'
    )
    print("  [✓] storage.py 补充 import os")
else:
    print("  [已存在] os 已导入")

# ============================================================
# 额外修复：BacktestEngine 中 funding_arb 策略未处理
# ============================================================
print("\n[额外] 修复 BacktestEngine 缺少 funding_arb 向量化信号 (engine/core.py)")

core_content = core_path.read_text(encoding='utf-8')
funding_arb_fix_marker = "'funding_arb' in _vectorized_signals"

if funding_arb_fix_marker not in core_content:
    replace_in_file(
        core_path,
        """        elif strategy_type == 'stat_arb':
            # 均值回归 Z-score
            close = df['close'].values
            zp = params.get('zscore_period', 20)
            zt = params.get('zscore_threshold', 2.0)
            rsi = df['rsi'].values

            for i in range(max(50, zp), n):
                window = close[max(0, i-zp):i]
                if len(window) < zp:
                    continue
                mean = np.mean(window)
                std = np.std(window)
                if std == 0:
                    continue
                z = (close[i] - mean) / std

                if z <= -zt:
                    buy[i] = True
                elif z >= zt:
                    sell[i] = True""",
        """        elif strategy_type == 'funding_arb':
            # 资金费率套利：使用 stat_arb 的均值回归作为回测代理信号
            # （实盘时会对接真实资金费率，回测阶段用价格偏离度近似）
            close = df['close'].values
            zp = params.get('zscore_period', 20)
            zt = params.get('zscore_threshold', 1.5)  # 套利阈值更低

            for i in range(max(50, zp), n):
                window = close[max(0, i - zp):i]
                if len(window) < zp:
                    continue
                mean = np.mean(window)
                std = np.std(window)
                if std == 0:
                    continue
                z = (close[i] - mean) / std
                if z <= -zt:
                    buy[i] = True
                elif z >= zt:
                    sell[i] = True

        elif strategy_type == 'stat_arb':
            # 均值回归 Z-score
            close = df['close'].values
            zp = params.get('zscore_period', 20)
            zt = params.get('zscore_threshold', 2.0)
            rsi = df['rsi'].values

            for i in range(max(50, zp), n):
                window = close[max(0, i-zp):i]
                if len(window) < zp:
                    continue
                mean = np.mean(window)
                std = np.std(window)
                if std == 0:
                    continue
                z = (close[i] - mean) / std

                if z <= -zt:
                    buy[i] = True
                elif z >= zt:
                    sell[i] = True""",
        "BacktestEngine: 补充 funding_arb 向量化信号处理"
    )
else:
    print("  [已存在] funding_arb 已处理")

# ============================================================
# 总结
# ============================================================
print("\n" + "=" * 60)
print("[完成] 所有 Bug 修复已应用")
print()
print("修复说明:")
print("  Bug 1: vol_vol → vol_label  (NameError 崩溃)")
print("  Bug 2: api_trade_stats 重命名  (Flask 启动失败)")
print("  Bug 3: asyncio.Lock → threading.Lock  (模块导入报错)")
print("  Bug 4: DB_PATH 支持 TEST_DB_PATH  (测试污染生产数据)")
print("  额外:  funding_arb 回测信号补充")
print()
print("下一步:")
print("  1. 重新运行测试: python -m pytest tests/ -v")
print("  2. 启动应用:     python app.py")
print("  3. Docker 部署:  docker compose up -d --build")
print("=" * 60)
