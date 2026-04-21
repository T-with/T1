# Phase 3 — 日志 + 指标 + 报警 + Grafana

## 📦 文件清单

```
phase3/
├── engine/
│   ├── logging_setup.py         # structlog JSON 日志 + 上下文 + 脱敏
│   ├── metrics.py               # Prometheus 指标定义 + 快照线程
│   └── alerts.py                # 多渠道告警 (TG/Webhook/SMTP) + 去重
├── monitoring/
│   ├── prometheus/
│   │   └── prometheus.yml       # 抓取配置
│   └── grafana/
│       ├── datasources/         # 数据源自动配置
│       └── dashboards/
│           └── trading-overview.json   # 预置 dashboard (25 面板)
├── docker-compose.monitoring.yml       # Prometheus + Grafana
├── apply_phase3.py                     # 自动应用到 app.py
├── apply_phase3_instrumentation.py     # LiveTrader 业务指标埋点
├── test_phase3.py                      # 14 个测试,已验证全过
├── alerts.env.example                  # 告警渠道配置示例
├── requirements.txt                    # 新增 structlog + prometheus-client
└── README.md                           # 本文件
```

---

## 🚀 应用步骤

### 步骤 1 — 安装新依赖

```bash
cp phase3/requirements.txt .
pip install -r requirements.txt
```

### 步骤 2 — 放新模块

```bash
cp phase3/engine/logging_setup.py engine/
cp phase3/engine/metrics.py engine/
cp phase3/engine/alerts.py engine/
cp phase3/test_phase3.py tests/
```

### 步骤 3 — 自动应用 app.py 改动

```bash
python3 phase3/apply_phase3.py
python3 phase3/apply_phase3_instrumentation.py
```

### 步骤 4 — 跑测试验证

```bash
pytest tests/test_phase3.py -v
# 14 passed

pytest tests/ -v
# 全部 (48 原 + Phase 2 + 14 新) = 70+ 应全过
```

### 步骤 5 — 配置告警 (可选)

编辑 `.env`,追加想用的渠道(复制 `alerts.env.example` 参考):

```bash
# 最简单:Telegram (5 分钟搞定)
ALERT_TELEGRAM_BOT_TOKEN=xxx
ALERT_TELEGRAM_CHAT_ID=xxx

# 或者飞书群机器人
ALERT_WEBHOOK_TYPE=feishu
ALERT_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 或者邮件
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
...
```

### 步骤 6 — 复制监控栈配置

```bash
cp phase3/docker-compose.monitoring.yml .
cp -r phase3/monitoring ./
```

### 步骤 7 — 启动

```bash
# 只启 app (观测性已就绪,但没 Grafana)
docker compose up -d --build

# 带 Grafana 启动
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --build
```

### 步骤 8 — 验证

```bash
# 1. /metrics 能访问 (不需要认证)
curl http://localhost:8080/metrics | head -30
# 应看到 strategies_running, http_requests_total 等

# 2. 告警状态
curl -u admin:PASS http://localhost:8080/api/alerts/status
# → {"channels":["telegram","webhook"], ...}

# 3. 手动发一条测试告警 (会真的推到你配的渠道)
curl -u admin:PASS -X POST http://localhost:8080/api/alerts/test \
  -H "Content-Type: application/json" \
  -d '{"title":"测试","message":"收到就说明通了","severity":"critical"}'

# 4. Grafana (如果起了监控栈)
#    浏览器: http://localhost:3000
#    默认 admin/admin (首次登录要求改密)
#    左侧菜单 Dashboards → 看到 "Trading Platform Overview"
```

---

## 🎯 实际能用到的场景

### 场景 1:策略亏到怀疑人生,想看到底啥时候开始的
```
Grafana → Trading Platform Overview → "策略回撤" 面板
能看到所有策略的回撤历史,鼠标悬停看任意时刻的值
```

### 场景 2:某策略突然不动了,要查最近日志
```bash
# 容器日志 (实时)
docker compose logs -f --tail 100 trading-platform | grep strategy_id=xxx

# 滚动文件日志 (历史)
cat logs/platform.jsonl | jq 'select(.strategy_id=="xxx")' | head
```
每条日志都是带 `strategy_id/trade_id/request_id` 的 JSON,可以用 jq / ELK / Loki 随便查。

### 场景 3:熔断了想立刻知道
配 Telegram → 任何 critical/warning 级风控事件 都会自动推,带"为什么"、"哪个策略"、"当前值"。去重冷却 5 分钟,不会刷屏。

### 场景 4:API 延迟异常要定位
```
Grafana → HTTP 延迟 P95/P99
交易所 API 延迟 P95 → 看是 /api/strategies 慢还是 binance fetch_ohlcv 慢
```

---

## 📊 Grafana Dashboard 包含的面板

### 顶部概览 (8 个 stat)
- 运行中策略数(纸面/实盘)
- 熔断状态
- Kill Switch
- 暂停策略数
- 总权益
- 最大回撤

### 策略权益 (2 个 timeseries)
- 各策略权益走势
- 策略回撤走势

### 交易与订单 (4 个)
- 交易速率(按 win/loss/break_even)
- 订单成交耗时 P50/P95
- 单笔盈亏分布热力图
- 订单滑点 P95

### 风控与告警 (3 个)
- 风控事件速率(按类型)
- 告警发送(按渠道/状态)
- 过去 1h 风控事件数(按级别)

### HTTP 与外部调用 (4 个)
- HTTP 请求速率(按状态码)
- HTTP 延迟 P50/P95/P99
- 交易所 API 延迟 P95
- 交易所 API 错误率

---

## ⚠️ 注意事项

1. **`/metrics` 端点故意不走 Basic Auth** — Prometheus 要能抓。
   生产环境请用防火墙限制 9090/3000 只允许内网访问(已经在 compose 里绑定到 `127.0.0.1`)。

2. **告警去重冷却默认 5 分钟** — 同一指纹的告警在这 5 分钟内只发一次。
   改 `AlertManager(dedup_cooldown_sec=...)` 调整。

3. **日志文件滚动** — `logs/platform.jsonl`,单文件 50MB,保留 10 个,总共 500MB 占用。
   容器重启/崩溃 logs 目录被 volume 挂出,不会丢。

4. **指标快照每 15 秒一次** — 权益/持仓/回撤这些 Gauge 不会每次事件都更新,
   而是后台线程定期扫描 LiveTrader。节省 CPU,符合 Prometheus pull 模型语义。

5. **Kill Switch 激活后 `/api/alerts/*` 依然工作** — 报警本身不应受限于紧急停止。

---

## 🐛 常见问题

### Q: 启动后 `/metrics` 403?
A: 不应该,它在 `auth_check` 的白名单里。检查 `apply_phase3.py` 步骤 4 是否应用成功。

### Q: Grafana 登不上?
A: 默认 `admin/admin`,首次登录强制改密。也可以在 `.env` 加 `GRAFANA_PASS=xxx`。

### Q: Prometheus `UP` 指标显示 trading-platform down?
A: 检查 docker network,两个 compose 文件必须在同一 network(默认 bridge 可能不行)。
```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
# 两个文件一起起,它们自动在同一 network
```

### Q: 告警渠道没配,会不会刷 warning 日志?
A: 只在启动时 warn 一次,之后 send() 直接返回 False,不刷日志。

### Q: structlog 看着没有彩色
A: 在 docker 里 stdout 不是 TTY,自动切 JSON 模式。本地 `python app.py` 跑的话会有彩色。

---

## 下一步 (Phase 4)

- Walk-forward 分离 OOS 回测(当前 AI 多因子有数据泄露风险)
- 策略版本管理(config hash → 结果挂钩)
- 特征泄露检查
- 模型漂移检测
