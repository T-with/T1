# MyTradingPlatform — 自研量化交易平台

24/7 自动化加密货币量化交易系统。

## 访问

- **Web 界面:** http://47.84.119.2:8080

## 功能

- 📊 **Dashboard** — 总权益/盈亏/持仓概览
- 🧠 **策略管理** — 创建/编辑/启动/停止策略
- 🔄 **回测系统** — 历史数据验证，含手续费/滑点/杠杆
- ⚡ **实盘交易** — 模拟盘 + 实盘，24/7 自动运行
- 🛡️ **风控系统** — 止损/止盈/追踪止损/最大回撤
- 📱 **实时监控** — 持仓/交易日志实时刷新

## 内置策略

| 策略 | 说明 |
|------|------|
| MACD 金叉/死叉 | 经典趋势跟踪 |
| RSI 超买超卖 | 均值回归 |
| 布林带突破 | 波动率突破 |
| 双均线交叉 | 简单趋势跟踪 |

## 支持交易所

Binance / OKX / Bybit / Bitget / KuCoin / Gate.io

## 部署

详细部署流程见 **[DEPLOY.md](DEPLOY.md)**

```bash
# 快速启动
cp .env.example .env        # 修改密码
docker compose up -d --build
```

## 项目结构

```
MyTradingPlatform/
├── app.py              # Flask Web 服务 + API
├── engine/
│   └── core.py         # 交易引擎 (指标/策略/回测/实盘)
├── templates/          # Web 页面
│   ├── base.html       # 布局模板
│   ├── index.html      # Dashboard
│   ├── strategy.html   # 策略管理
│   ├── backtest.html   # 回测
│   ├── live.html       # 实盘交易
│   └── settings.html   # 设置
├── data/               # 运行数据 (持久化)
├── Dockerfile
└── docker-compose.yml
```
