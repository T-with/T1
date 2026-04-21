# MyTradingPlatform — Bug 修复说明

## 修复的 Bug 清单

---

## Bug 1：`vol_vol` 未定义变量
**文件**: `engine/core.py`  
**位置**: `SmartDCAStrategy._compute_weight()` 方法末尾  
**严重程度**: 🔴 **致命** — 每次调用 DCA 策略时必然崩溃  

### 问题代码
```python
market_state = f'rsi_{rsi_label}_vol_{vol_vol if False else vol_label}'
```

`vol_vol` 在整个方法中从未定义，`if False` 永远不成立，所以每次都会执行 `vol_vol`，触发 `NameError`。

### 修复代码
```python
market_state = f'rsi_{rsi_label}_vol_{vol_label}'
```

---

## Bug 2：Flask 路由函数名冲突
**文件**: `app.py`  
**位置**: Phase 2 注入的路由区域  
**严重程度**: 🔴 **致命** — 应用无法启动  

### 问题原因
`app.py` 中存在两个同名的 Flask 视图函数 `api_trade_stats`：

```python
# 原有（数据层 API）
@app.route('/api/data/trades/<symbol>', methods=['GET'])
def api_trade_stats(symbol): ...

# Phase 2 注入（交易统计 API）
@app.route('/api/trades/stats/<sid>', methods=['GET'])
def api_trade_stats(sid): ...  # ← 重名！
```

Flask 启动时会抛出：
```
AssertionError: View function mapping is overwriting an existing endpoint function: api_trade_stats
```

### 修复方案
将 Phase 2 注入的函数重命名为 `api_strategy_trade_stats`：
```python
@app.route('/api/trades/stats/<sid>', methods=['GET'])
def api_strategy_trade_stats(sid): ...
```

---

## Bug 3：`asyncio.Lock()` 在无事件循环时创建
**文件**: `engine/data_ws.py`  
**位置**: `EventBus.__init__()`  
**严重程度**: 🟠 **严重** — Python 3.10+ 下导入模块时报错  

### 问题代码
```python
class EventBus:
    def __init__(self):
        ...
        self._lock = asyncio.Lock()  # 模块导入时执行，此时没有事件循环
```

Python 3.10 起，`asyncio.Lock()` 必须在运行中的事件循环里创建。  
`EventBus` 在 `data_manager = DataManager()` 时（模块级别）被实例化，  
此时没有事件循环，会触发：
```
DeprecationWarning: There is no current event loop
RuntimeError: no current event loop
```

而且这个 `_lock` 在代码里根本没被使用（`publish` 方法不用它），  
说明本来就是误加的。

### 修复方案
替换为 `threading.Lock()`（不需要事件循环）：
```python
self._lock = threading.Lock()
```

---

## Bug 4：`TEST_DB_PATH` 环境变量未生效
**文件**: `engine/storage.py`  
**位置**: 模块顶部 `DB_PATH` 定义  
**严重程度**: 🟡 **中等** — 导致测试污染生产数据库  

### 问题代码
```python
DB_PATH = DATA_DIR / 'platform.db'  # 硬编码，忽略环境变量
```

测试 fixture 里设置了：
```python
monkeypatch.setenv('TEST_DB_PATH', str(tmp_path / 'test.db'))
```

但 `DB_PATH` 在 `storage.py` 模块首次导入时已经固定了，  
后来修改环境变量也不会影响这个值。  
结果所有测试都往 `data/platform.db` 写数据，测试之间相互干扰，  
还可能破坏真实的策略数据。

### 修复方案
```python
import os
DB_PATH = Path(os.environ.get('TEST_DB_PATH', str(DATA_DIR / 'platform.db')))
```

---

## 额外修复：`funding_arb` 策略缺少回测支持
**文件**: `engine/core.py`  
**位置**: `BacktestEngine._vectorized_signals()`  
**严重程度**: 🟡 **中等** — 资金费率套利策略无法进行回测  

`StrategyEngine.STRATEGIES` 中列出了 `funding_arb`，  
`_vectorized_signals` 却没有对应的 `elif` 分支，  
运行回测时产生空信号（不崩溃但无意义）。

### 修复方案
补充 `funding_arb` 分支，使用价格 Z-score 作为回测代理信号：
```python
elif strategy_type == 'funding_arb':
    close = df['close'].values
    zp = params.get('zscore_period', 20)
    zt = params.get('zscore_threshold', 1.5)
    for i in range(max(50, zp), n):
        window = close[max(0, i - zp):i]
        ...
```

---

## 如何应用修复

### 方法一：自动脚本（推荐）
```bash
# 将 apply_bugfix.py 放到项目根目录
python apply_bugfix.py
```

### 方法二：手动逐一修复
按照上面每个 Bug 的修复方案，对应文件直接修改。

---

## 修复后验证

```bash
# 运行全量测试
python -m pytest tests/ -v

# 预期结果（原有 + Phase 2 + Phase 3）
# 75+ tests passed, 0 failed

# 启动应用
python app.py
# 应该看到正常启动日志，没有 AssertionError
```
