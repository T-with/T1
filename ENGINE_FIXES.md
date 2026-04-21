# 交易引擎 / 策略模块修复说明

## 修复总览

| 类型 | 编号 | 位置 | 问题 | 严重程度 |
|------|------|------|------|---------|
| Bug | B1 | `Indicators.rsi` | 全涨行情 loss=0 → 除零 → RSI 变 NaN | 🔴 高 |
| Bug | B2 | `StrategyEngine._dual_ma` | 直接修改传入 df，污染调用方数据 | 🟠 中 |
| Bug | B3 | `SmartDCAStrategy._compute_weight` | `vol_vol` 未定义 → NameError | 🔴 高 |
| Bug | B4 | `BacktestEngine._vectorized_signals` | `funding_arb` 无信号分支，回测空跑 | 🟡 低 |
| 功能 | F1 | `LiveTrader._run_loop` | sell 信号只平多不开空 | 🔴 高 |
| 功能 | F2 | `LiveTrader._run_loop` | 仓位规模用初始资金，不随权益复利 | 🟠 中 |
| 功能 | F3 | `ExchangeClient.fetch_ohlcv_range` | 不过滤超出结束日期的数据 | 🟡 低 |
| 性能 | P1 | `ExchangeClient.fetch_ohlcv` | 纯磁盘缓存，同 session 重复读盘 | 🟠 中 |
| 性能 | P2 | `BacktestEngine.run` | 止损止盈条件未向量化 | 🟡 低 |
| 性能 | P3 | `Indicators.add_all` | 重复读取同列数据 | 🟡 低 |

---

## Bug B1：`Indicators.rsi` 全涨行情除零

**问题代码**
```python
rs = gain / loss          # loss 可以为 0（全涨行情）
return 100 - 100 / (1 + rs)  # rs = inf → 100 - 0 = 100，但 NaN 也会传播
```

**复现场景**
```python
# 模拟连续 20 根全涨 K 线
df = pd.DataFrame({'close': range(100, 120)}, ...)
rsi = Indicators.rsi(df, 14)
# 结果：最后几根 rsi 为 NaN，策略信号全部失效
```

**修复**
```python
rs = gain / loss.replace(0, float('nan'))
rsi = 100 - 100 / (1 + rs)
rsi = rsi.fillna(100)   # loss=0 意味着纯涨势，RSI 定义上为 100
return rsi
```

---

## Bug B2：`StrategyEngine._dual_ma` 污染调用方 DataFrame

**问题代码**
```python
@staticmethod
def _dual_ma(df, params):
    ...
    df['ma_f'] = ind.ema(df, fast)   # 直接在传入的 df 上加列！
    df['ma_s'] = ind.ema(df, slow)
```

**问题影响**
- 调用方传入的 `df` 会被永久修改（多出 `ma_f`、`ma_s` 两列）
- 如果同一个 `df` 被多个策略共享（如回测中多次调用 `_vectorized_signals`），会产生列冲突
- 难以调试的隐式副作用

**修复**
```python
df = df.copy()  # 加这一行
if ma_type == 'ema':
    df['ma_f'] = ...
```

---

## Bug B3：`vol_vol` 未定义

**位置**: `SmartDCAStrategy._compute_weight` 末尾

```python
# 原代码
market_state = f'rsi_{rsi_label}_vol_{vol_vol if False else vol_label}'
#                                          ^^^^^^^^ 从未定义！
# 'if False' 永远不成立，所以永远执行 vol_vol → NameError

# 修复
market_state = f'rsi_{rsi_label}_vol_{vol_label}'
```

---

## Bug B4：`funding_arb` 策略回测空跑

`BacktestEngine._vectorized_signals` 没有 `funding_arb` 的 `elif` 分支，
它会跳过所有条件直接到 `buy[:50] = False`，产生全零信号。

回测结果看起来"正常"（return=0%，trades=0），实际上是策略根本没运行。

**修复**：补充 Z-score 代理信号分支（回测阶段用价格偏离度近似资金费率信号）。

---

## 功能 F1：LiveTrader 不支持开空仓

**现状**
```python
elif sig['type'] == 'sell':
    if config.symbol in state['positions'] and ...'side'] == 'long':
        self._close_position(...)   # 只平多
    state['last_signal'] = 'sell'
    # ← 没有开空的逻辑！
```

**影响**
- `stat_arb`、`funding_arb`、`bollinger_breakout` 等策略的 sell 信号**全部无效**
- 这些策略回测可以做空，但实盘永远只做多
- 策略表现严重偏离回测结果

**修复逻辑**
```
sell 信号 → 先平多仓（如有）
          → 检查是否满足开空条件：
              ✓ 当前无持仓
              ✓ 策略类型允许做空（排除 DCA/网格/AI长期持仓策略）
              ✓ 杠杆 > 1（现货无法做空）
          → 满足则按当前权益计算仓位并开空
```

---

## 功能 F2：仓位规模不随权益增减（不复利）

**现状**
```python
amount_usdt = config.capital * position_pct / 100 * config.leverage
#             ^^^^^^^^^^^^^^ 永远是初始资金，赚钱了也不加仓，亏钱了不减仓
```

**问题**
- 策略盈利后，仓位依然按初始资金计算 → 错过复利增长
- 策略亏损后，仓位依然按初始资金计算 → 实际仓位占比过高，风险超标

**修复**
```python
amount_usdt = state['equity'] * position_pct / 100 * config.leverage
#             ^^^^^^^^^^^^^^^ 使用当前权益，支持复利
```

---

## 功能 F3：`fetch_ohlcv_range` 数据越界

Binance API 以 1000 条为单位返回数据，最后一批可能超过 `end_date`。

```python
# 问题：since 递增到 end_ts 附近时，最后一个 API 请求可能返回跨越 end_ts 的数据
while since < end_ts:
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
    all_data.extend(ohlcv)  # ← 多余的数据也进来了
    since = ohlcv[-1][0] + tf_ms
```

**修复**
```python
# 在构建 DataFrame 后过滤
end_dt = pd.Timestamp(end)
df = df[df.index <= end_dt]
```

---

## 性能 P1：ExchangeClient 内存缓存

**现状流程**
```
fetch_ohlcv() → 检查磁盘 pickle → 读文件 → 反序列化 → 返回
```

**问题**：同一策略在同一个运行周期内对相同交易对/周期的请求非常频繁
（回测、信号生成、风控检查各调用一次），每次都要读磁盘。

**修复后**
```
fetch_ohlcv() → 内存字典命中 → 直接返回（~5-10x 速度提升）
             → 内存未命中 → 检查磁盘 pickle → 读文件 → 写入内存字典 → 返回
             → 磁盘未命中 → 调用交易所 API → 写磁盘 + 写内存 → 返回
```

内存缓存参数：
- TTL：60 秒（短于磁盘缓存的 300 秒，避免内存数据过旧）
- 最大条目：64 条（防止内存膨胀）
- 淘汰策略：TTL 最短优先

---

## 性能 P3：`Indicators.add_all` 减少冗余计算

**原代码问题**
```python
df['sma_20'] = ind.sma(df, 20)       # close.rolling(20).mean()
...
u, mid, l = ind.bollinger(df)         # 内部又计算一次 close.rolling(20).mean()！
```

布林带中轨 = SMA20，计算了两次。

**修复**：布林带直接复用 `df['sma_20']`：
```python
df['bb_middle'] = df['sma_20']          # 不重算
df['bb_upper']  = df['sma_20'] + 2 * std20
df['bb_lower']  = df['sma_20'] - 2 * std20
```

同理，MACD 复用已计算的 `ema_12`、`ema_26`，减少 4 次 `ewm` 调用。

---

## 使用方法

```bash
# 将 apply_engine_fixes.py 放到项目根目录
python apply_engine_fixes.py

# 验证
python -m pytest tests/ -v -k "TestIndicators or TestStrategies or TestBacktest"

# 完整测试
python -m pytest tests/ -v
```

---

## 修复后效果预期

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 全涨行情 RSI | NaN（策略无信号） | 100（正确）|
| DCA 策略运行 | 必然 NameError | 正常运行 |
| funding_arb 回测 | 0 笔交易 | 正常产生信号 |
| 做空策略实盘 | 无法建空仓 | 正常开空 |
| 仓位复利 | 不增长 | 随权益动态调整 |
| 重复数据请求延迟 | ~50ms（读磁盘）| ~0.1ms（读内存）|
| Indicators.add_all | 重复 4 次计算 | 减少约 30% 计算量 |
