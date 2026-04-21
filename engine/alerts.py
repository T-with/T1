"""
engine/alerts.py — 多渠道报警系统

特性:
1. 多渠道: Telegram / Webhook (飞书/钉钉/Discord) / SMTP,都支持
2. 去重 + 限流: 同一告警指纹在冷却期内只发一次,避免刷屏
3. 严重度分级: info / warning / critical,不同级别走不同渠道
4. 异步发送: 不阻塞主流程,发送失败只记日志不抛异常
5. 通过环境变量配置,不用改代码加渠道

环境变量:
    # Telegram (最简单)
    ALERT_TELEGRAM_BOT_TOKEN=123:abc
    ALERT_TELEGRAM_CHAT_ID=-1001234567

    # 飞书/钉钉/Discord Webhook
    ALERT_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
    ALERT_WEBHOOK_TYPE=feishu     # feishu / dingtalk / discord / generic

    # SMTP
    ALERT_SMTP_HOST=smtp.gmail.com
    ALERT_SMTP_PORT=587
    ALERT_SMTP_USER=you@gmail.com
    ALERT_SMTP_PASS=app_password
    ALERT_SMTP_FROM=you@gmail.com
    ALERT_SMTP_TO=dest@example.com

    # 过滤 — 只发这些级别以上
    ALERT_MIN_SEVERITY=warning   # info/warning/critical
"""

import os
import json
import time
import logging
import hashlib
import threading
import queue
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Callable
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# 基础类型
# ============================================================

class Severity(Enum):
    INFO = 'info'
    WARNING = 'warning'
    CRITICAL = 'critical'

    def __lt__(self, other):
        order = {'info': 0, 'warning': 1, 'critical': 2}
        return order[self.value] < order[other.value]


_SEVERITY_EMOJI = {
    Severity.INFO: 'ℹ️',
    Severity.WARNING: '⚠️',
    Severity.CRITICAL: '🚨',
}


@dataclass
class Alert:
    title: str
    message: str
    severity: Severity = Severity.WARNING
    source: str = ''                   # 'risk_manager' / 'kill_switch' / 'reconcile' / ...
    tags: Dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def fingerprint(self) -> str:
        """用于去重的指纹"""
        key = f"{self.source}:{self.title}:{sorted(self.tags.items())}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    def format_plain(self) -> str:
        emoji = _SEVERITY_EMOJI.get(self.severity, '')
        ts = datetime.fromtimestamp(self.timestamp).strftime('%Y-%m-%d %H:%M:%S')
        lines = [
            f"{emoji} [{self.severity.value.upper()}] {self.title}",
            f"时间: {ts}",
            f"来源: {self.source}" if self.source else "",
            "",
            self.message,
        ]
        if self.tags:
            lines.append("")
            for k, v in self.tags.items():
                lines.append(f"• {k}: {v}")
        return "\n".join(l for l in lines if l is not None)


# ============================================================
# 渠道抽象
# ============================================================

class Channel:
    name = 'base'

    def send(self, alert: Alert) -> bool:
        raise NotImplementedError

    @classmethod
    def from_env(cls) -> Optional['Channel']:
        """按环境变量尝试构造,不满足配置就返回 None"""
        raise NotImplementedError


class TelegramChannel(Channel):
    name = 'telegram'

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, alert: Alert) -> bool:
        try:
            payload = json.dumps({
                'chat_id': self.chat_id,
                'text': alert.format_plain(),
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }).encode('utf-8')
            req = urllib.request.Request(
                self.url, data=payload,
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"[telegram] send failed: {e}")
            return False

    @classmethod
    def from_env(cls):
        token = os.environ.get('ALERT_TELEGRAM_BOT_TOKEN', '').strip()
        chat_id = os.environ.get('ALERT_TELEGRAM_CHAT_ID', '').strip()
        if token and chat_id:
            return cls(token, chat_id)
        return None


class WebhookChannel(Channel):
    """
    通用 webhook — 支持飞书/钉钉/Discord/任意自定义
    根据 ALERT_WEBHOOK_TYPE 决定 payload 格式
    """
    name = 'webhook'

    def __init__(self, url: str, hook_type: str = 'generic'):
        self.url = url
        self.hook_type = hook_type

    def send(self, alert: Alert) -> bool:
        try:
            payload = self._build_payload(alert)
            req = urllib.request.Request(
                self.url,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            logger.error(f"[webhook:{self.hook_type}] send failed: {e}")
            return False

    def _build_payload(self, alert: Alert) -> Dict:
        text = alert.format_plain()

        if self.hook_type == 'feishu':
            # 飞书群机器人
            return {
                'msg_type': 'text',
                'content': {'text': text},
            }

        if self.hook_type == 'dingtalk':
            # 钉钉群机器人
            return {
                'msgtype': 'text',
                'text': {'content': text},
            }

        if self.hook_type == 'discord':
            color = {
                Severity.INFO: 0x3498db,
                Severity.WARNING: 0xf39c12,
                Severity.CRITICAL: 0xe74c3c,
            }[alert.severity]
            return {
                'embeds': [{
                    'title': f"{_SEVERITY_EMOJI.get(alert.severity,'')} {alert.title}",
                    'description': alert.message,
                    'color': color,
                    'fields': [{'name': k, 'value': str(v), 'inline': True}
                               for k, v in alert.tags.items()][:25],
                    'timestamp': datetime.fromtimestamp(alert.timestamp).isoformat(),
                    'footer': {'text': alert.source},
                }],
            }

        if self.hook_type == 'slack':
            color = {
                Severity.INFO: 'good',
                Severity.WARNING: 'warning',
                Severity.CRITICAL: 'danger',
            }[alert.severity]
            return {
                'attachments': [{
                    'color': color,
                    'title': alert.title,
                    'text': alert.message,
                    'fields': [{'title': k, 'value': str(v), 'short': True}
                               for k, v in alert.tags.items()],
                    'ts': alert.timestamp,
                }],
            }

        # 通用 — 原样送过去
        return {
            'title': alert.title,
            'message': alert.message,
            'severity': alert.severity.value,
            'source': alert.source,
            'tags': alert.tags,
            'timestamp': alert.timestamp,
        }

    @classmethod
    def from_env(cls):
        url = os.environ.get('ALERT_WEBHOOK_URL', '').strip()
        hook_type = os.environ.get('ALERT_WEBHOOK_TYPE', 'generic').strip()
        if url:
            return cls(url, hook_type)
        return None


class SMTPChannel(Channel):
    name = 'smtp'

    def __init__(self, host: str, port: int, user: str, password: str,
                 sender: str, recipients: List[str]):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender
        self.recipients = recipients

    def send(self, alert: Alert) -> bool:
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header
        try:
            msg = MIMEText(alert.format_plain(), 'plain', 'utf-8')
            msg['Subject'] = Header(
                f"[{alert.severity.value.upper()}] {alert.title}", 'utf-8'
            )
            msg['From'] = self.sender
            msg['To'] = ', '.join(self.recipients)

            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.starttls()
                if self.user:
                    server.login(self.user, self.password)
                server.sendmail(self.sender, self.recipients, msg.as_string())
            return True
        except Exception as e:
            logger.error(f"[smtp] send failed: {e}")
            return False

    @classmethod
    def from_env(cls):
        host = os.environ.get('ALERT_SMTP_HOST', '').strip()
        if not host:
            return None
        return cls(
            host=host,
            port=int(os.environ.get('ALERT_SMTP_PORT', '587')),
            user=os.environ.get('ALERT_SMTP_USER', '').strip(),
            password=os.environ.get('ALERT_SMTP_PASS', ''),
            sender=os.environ.get('ALERT_SMTP_FROM', '').strip(),
            recipients=[e.strip() for e in
                        os.environ.get('ALERT_SMTP_TO', '').split(',')
                        if e.strip()],
        )


# ============================================================
# 管理器 — 去重 + 限流 + 异步分发
# ============================================================

class AlertManager:
    """
    告警分发器

    职责:
    1. 维护一组 Channel
    2. 对每个进来的 Alert 做去重 (相同指纹 cooldown 内丢弃)
    3. 异步推送到所有 enabled 的 channel
    """

    def __init__(self, dedup_cooldown_sec: int = 300,
                 min_severity: Severity = Severity.WARNING,
                 metric_callback: Optional[Callable] = None):
        self.channels: List[Channel] = []
        self.dedup_cooldown_sec = dedup_cooldown_sec
        self.min_severity = min_severity
        self._recent_fingerprints: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=1000)
        self._worker_started = False
        self._metric_callback = metric_callback  # 用于 Prometheus

    def add_channel(self, channel: Channel):
        self.channels.append(channel)
        logger.info(f"Alert channel added: {channel.name}")

    def load_from_env(self):
        """从环境变量自动加载所有可用渠道"""
        for cls in (TelegramChannel, WebhookChannel, SMTPChannel):
            ch = cls.from_env()
            if ch:
                self.add_channel(ch)

        # 读取最小严重度
        min_sev = os.environ.get('ALERT_MIN_SEVERITY', 'warning').lower()
        if min_sev in ('info', 'warning', 'critical'):
            self.min_severity = Severity(min_sev)

        if not self.channels:
            logger.warning("No alert channels configured (check ALERT_* env vars)")

        return len(self.channels)

    def send(self, alert: Alert) -> bool:
        """提交一个 alert (非阻塞)"""
        # 过滤等级
        if alert.severity < self.min_severity:
            return False

        # 去重
        fp = alert.fingerprint()
        now = time.time()
        with self._lock:
            last = self._recent_fingerprints.get(fp, 0)
            if now - last < self.dedup_cooldown_sec:
                return False
            self._recent_fingerprints[fp] = now
            # 清理超过 1 小时的旧指纹
            if len(self._recent_fingerprints) > 1000:
                self._recent_fingerprints = {
                    k: v for k, v in self._recent_fingerprints.items()
                    if now - v < 3600
                }

        if not self.channels:
            return False

        self._ensure_worker()
        try:
            self._queue.put_nowait(alert)
            return True
        except queue.Full:
            logger.warning("Alert queue full, dropping alert")
            return False

    def _ensure_worker(self):
        """首次调用时启动后台发送线程"""
        if self._worker_started:
            return
        with self._lock:
            if self._worker_started:
                return
            self._worker_started = True
            t = threading.Thread(
                target=self._worker_loop, daemon=True, name='alert-sender'
            )
            t.start()

    def _worker_loop(self):
        while True:
            alert = self._queue.get()
            for ch in self.channels:
                ok = False
                try:
                    ok = ch.send(alert)
                except Exception as e:
                    logger.error(f"Channel {ch.name} raised: {e}")
                # 指标回调
                if self._metric_callback:
                    try:
                        self._metric_callback(ch.name, alert.severity.value,
                                             'sent' if ok else 'failed')
                    except Exception:
                        pass

    def shortcut(self, title: str, message: str, severity: str = 'warning',
                 source: str = '', **tags):
        """便捷方法"""
        sev = Severity(severity.lower()) if severity.lower() in ('info','warning','critical') else Severity.WARNING
        return self.send(Alert(
            title=title, message=message, severity=sev,
            source=source, tags={k: str(v) for k, v in tags.items()},
        ))


# ============================================================
# 全局单例
# ============================================================

# 晚一点在 app.py 里调用 load_from_env() 才绑定 metrics
alert_manager = AlertManager()


# ============================================================
# 风控事件 → 告警转换
# ============================================================

def risk_event_to_alert(event) -> Optional[Alert]:
    """把 RiskEvent 映射成 Alert

    配合 RiskManager.register_callback(lambda e: alert_manager.send(risk_event_to_alert(e)))
    """
    # event 可能是 RiskEvent dataclass 或 dict (来自 DB)
    event_type = getattr(event, 'type', None) or event.get('event_type', '')
    if hasattr(event_type, 'value'):
        event_type = event_type.value
    level = getattr(event, 'level', None) or event.get('level', 'warning')
    if hasattr(level, 'value'):
        level = level.value

    # 只对 warning+ 发告警
    level_sev_map = {
        'normal': Severity.INFO,
        'warning': Severity.WARNING,
        'danger': Severity.WARNING,
        'critical': Severity.CRITICAL,
        'circuit_breaker': Severity.CRITICAL,
    }
    severity = level_sev_map.get(level, Severity.WARNING)
    if severity < Severity.WARNING:
        return None

    title_map = {
        'stop_loss': '止损触发',
        'take_profit': '止盈触发',
        'trailing_stop': '追踪止损触发',
        'max_drawdown': '最大回撤超限',
        'volatility_spike': '波动率突增',
        'flash_crash': '闪崩检测',
        'consecutive_losses': '连续亏损',
        'circuit_breaker': '全局熔断',
        'reconcile_size_mismatch': '持仓数量不一致',
        'reconcile_side_mismatch': '持仓方向不一致',
        'reconcile_missing_local': '未知持仓 (交易所有但本地无)',
        'reconcile_missing_remote': '持仓记录丢失',
        'kill_switch': 'Kill Switch 触发',
    }
    title = title_map.get(event_type, f'风控事件: {event_type}')

    msg = getattr(event, 'message', None) or event.get('message', '')
    strategy_id = getattr(event, 'strategy_id', None) or event.get('strategy_id', '')
    symbol = getattr(event, 'symbol', None) or event.get('symbol', '')

    tags = {}
    if strategy_id:
        tags['strategy'] = strategy_id
    if symbol:
        tags['symbol'] = symbol

    data = getattr(event, 'data', None) or event.get('data', {}) or {}
    for k in ('pnl_pct', 'drawdown_pct', 'action_taken', 'action_recommended'):
        if k in data:
            tags[k] = str(data[k])

    return Alert(
        title=title, message=msg,
        severity=severity, source='risk_manager', tags=tags,
    )
