# MyTradingPlatform 部署指南

## 目录

- [环境要求](#环境要求)
- [快速部署（Docker）](#快速部署docker)
- [手动部署](#手动部署)
- [配置说明](#配置说明)
- [安全加固](#安全加固)
- [运维管理](#运维管理)
- [常见问题](#常见问题)

---

## 环境要求

### Docker 部署（推荐）

| 项目 | 最低要求 |
|------|---------|
| 系统 | Ubuntu 20.04+ / Debian 11+ / CentOS 8+ |
| CPU | 1 核 |
| 内存 | 512MB |
| 硬盘 | 5GB 可用空间 |
| Docker | 20.10+ |
| Docker Compose | 2.0+ |

### 手动部署

| 项目 | 最低要求 |
|------|---------|
| Python | 3.10+ |
| pip | 21.0+ |

---

## 快速部署（Docker）

### 1. 安装 Docker

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker

# 验证
docker --version
docker compose version
```

### 2. 克隆代码

```bash
cd /opt
git clone https://github.com/T-with/T1.git
cd T1
```

### 3. 配置凭据

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑配置（务必修改密码！）
cat > .env << 'EOF'
ADMIN_USER=admin
ADMIN_PASS=你的强密码_至少16位
EOF
```

> ⚠️ **绝对不要用默认密码 `changeme` 上生产！**

### 4. 启动服务

```bash
docker compose up -d --build
```

首次构建约需 1-2 分钟（安装依赖）。

### 5. 验证部署

```bash
# 检查容器状态
docker compose ps

# 查看日志
docker compose logs -f --tail 50

# 健康检查
curl -s http://localhost:8080/health
# 预期输出: {"status":"ok"}
```

### 6. 访问平台

浏览器打开：

```
http://你的服务器IP:8080
```

弹出登录框，输入你设置的 `ADMIN_USER` 和 `ADMIN_PASS`。

---

## 手动部署

适用于不想用 Docker 的场景。

### 1. 安装 Python 依赖

```bash
cd /opt/T1

# 创建虚拟环境（推荐）
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
export ADMIN_USER=admin
export ADMIN_PASS=你的强密码
```

或写入 systemd service 文件（见下文）。

### 3. 测试启动

```bash
# 开发模式
python app.py

# 生产模式（推荐）
gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 app:app
```

### 4. 注册为系统服务（可选）

```bash
cat > /etc/systemd/system/trading-platform.service << 'EOF'
[Unit]
Description=MyTradingPlatform
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/T1
Environment=ADMIN_USER=admin
Environment=ADMIN_PASS=你的强密码
ExecStart=/opt/T1/venv/bin/gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now trading-platform
systemctl status trading-platform
```

---

## 配置说明

### 文件结构

```
T1/
├── .env                  # 环境变量（不提交到 Git）
├── data/                 # 运行时数据（持久化）
│   ├── strategies.json   # 策略配置
│   ├── exchange.json     # 交易所配置（密钥已加密）
│   ├── .enc_key          # 加密密钥（自动生成）
│   └── cache/            # K线数据缓存
├── logs/
│   └── platform.log      # 运行日志
├── app.py
├── engine/core.py
└── docker-compose.yml
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_USER` | `admin` | Web 登录用户名 |
| `ADMIN_PASS` | `changeme` | Web 登录密码 |
| `TZ` | `Asia/Shanghai` | 时区 |

### 策略参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `symbol` | `BTC/USDT` | 交易对 |
| `timeframe` | `1h` | K线周期 |
| `capital` | `10000` | 初始资金 (USDT) |
| `leverage` | `1` | 杠杆倍数 |
| `position_size_pct` | `10` | 单次仓位占比 (%) |
| `stop_loss_pct` | `3` | 止损 (%) |
| `take_profit_pct` | `6` | 止盈 (%) |
| `trailing_stop_pct` | `2` | 追踪止损回撤 (%) |
| `max_drawdown_pct` | `20` | 最大回撤限制 (%) |
| `paper` | `true` | 模拟盘模式 |

---

## 安全加固

### 1. 防火墙

```bash
# 只允许特定 IP 访问 8080 端口
# Ubuntu (ufw)
sudo ufw allow from 你的IP地址 to any port 8080
sudo ufw enable

# CentOS (firewalld)
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="你的IP/32" port port="8080" protocol="tcp" accept'
sudo firewall-cmd --reload
```

### 2. Nginx 反向代理 + HTTPS（推荐）

```bash
apt install -y nginx certbot python3-certbot-nginx
```

```nginx
# /etc/nginx/sites-available/trading
server {
    listen 80;
    server_name trading.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name trading.yourdomain.com;

    # SSL（certbot 自动生成）
    ssl_certificate /etc/letsencrypt/live/trading.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/trading.yourdomain.com/privkey.pem;

    # 安全头
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 支持（如需实时推送）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

```bash
# 启用站点
ln -s /etc/nginx/sites-available/trading /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 申请 SSL 证书
certbot --nginx -d trading.yourdomain.com
```

### 3. 数据库备份

```bash
# 定期备份策略和交易所配置
cat > /opt/backup-trading.sh << 'EOF'
#!/bin/bash
BACKUP_DIR=/opt/backups/trading
mkdir -p $BACKUP_DIR
DATE=$(date +%Y%m%d_%H%M%S)
tar czf $BACKUP_DIR/trading_$DATE.tar.gz /opt/T1/data/
# 只保留最近 30 天
find $BACKUP_DIR -mtime +30 -delete
EOF

chmod +x /opt/backup-trading.sh

# 添加 crontab：每天凌晨 3 点备份
echo "0 3 * * * /opt/backup-trading.sh" | crontab -
```

---

## 运维管理

### Docker 常用命令

```bash
# 查看状态
docker compose ps

# 查看实时日志
docker compose logs -f

# 重启服务
docker compose restart

# 更新代码后重新构建
git pull
docker compose up -d --build

# 停止服务
docker compose down

# 进入容器调试
docker compose exec trading-platform bash
```

### 日志管理

```bash
# 查看应用日志
tail -f logs/platform.log

# 查看 Docker 日志
docker compose logs --tail 100 -f

# 日志轮转（Docker 默认自动管理）
# 手动清理
truncate -s 0 logs/platform.log
```

### 监控

```bash
# 简单健康检查脚本
cat > /opt/check-trading.sh << 'EOF'
#!/bin/bash
STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health)
if [ "$STATUS" != "200" ]; then
    echo "$(date) Trading platform DOWN, restarting..."
    cd /opt/T1 && docker compose restart
fi
EOF

chmod +x /opt/check-trading.sh
# 每 5 分钟检查
echo "*/5 * * * * /opt/check-trading.sh" | crontab -
```

---

## 常见问题

### Q: 忘记登录密码

编辑 `.env` 修改密码，然后重启：

```bash
nano .env          # 修改 ADMIN_PASS
docker compose restart
```

### Q: 交易所连接失败

1. 检查 API Key 权限（只需读取 + 交易，不要开提现）
2. 检查服务器是否能访问交易所（某些交易所有 IP 白名单）
3. 国内服务器可能需要代理，可在 `docker-compose.yml` 中添加 `HTTP_PROXY`

### Q: 回测数据获取缓慢

首次获取会从交易所拉取历史数据，之后有本地缓存（5 分钟有效）。大量回测建议：
- 缩短时间范围
- 使用更大的 K 线周期（4h 或 1d）

### Q: 模拟盘和实盘的区别

- **模拟盘**（默认）：信号正常生成，不发真实订单，适合验证策略逻辑
- **实盘**：使用交易所 API Key 真实下单，需要在策略中设置 `paper: false` 且配置好交易所 Key

### Q: 容器重启后策略还在吗

是的。策略配置保存在 `data/strategies.json`，通过 volume 持久化。但容器重启后需要手动重新「启动」策略。

---

## 版本历史

| 版本 | 日期 | 说明 |
|------|------|------|
| v1.1 | 2026-03-25 | 修复模拟盘崩溃、API密钥加密、Basic Auth |
| v1.0 | - | 初始版本 |
