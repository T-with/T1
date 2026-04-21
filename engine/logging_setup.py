"""
engine/logging_setup.py — 结构化日志

特性:
1. JSON 输出 (容器/ELK/CloudWatch 友好),开发时自动切彩色 console
2. 上下文变量 (contextvars): 一次 bind,后续全部日志自动带上
   例如进入一个 HTTP 请求时 bind(request_id=xxx),该请求所有日志都带 request_id
3. 与标准 logging 互操作: 原来的 logger.info() 调用依然工作,自动被格式化
4. 敏感字段自动脱敏: api_key / password / secret 这些 key 自动打码
5. 与 SQLite risk_event_repo 解耦 — logging 是 side-channel,不依赖 DB
"""

import logging
import os
import sys
import re
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

SENSITIVE_KEYS = re.compile(
    r'(api_key|api_secret|password|passphrase|token|authorization|cookie)',
    re.IGNORECASE,
)


def _redact_sensitive(logger, method_name, event_dict):
    """对敏感字段打码"""
    for k, v in list(event_dict.items()):
        if SENSITIVE_KEYS.search(k) and isinstance(v, str) and v:
            if len(v) > 8:
                event_dict[k] = v[:4] + '****' + v[-4:]
            else:
                event_dict[k] = '****'
    return event_dict


def _add_service_fields(logger, method_name, event_dict):
    """每条日志都带上服务身份"""
    event_dict.setdefault('service', 'trading-platform')
    event_dict.setdefault('env', os.environ.get('ENV', 'dev'))
    return event_dict


def setup_logging(level: str = None, json_mode: bool = None):
    """
    初始化全局日志配置

    Args:
        level: 日志级别 (默认读 LOG_LEVEL 环境变量,再默认 INFO)
        json_mode: True=JSON 输出,False=彩色 console,None=根据 TTY 自动判断
    """
    level = level or os.environ.get('LOG_LEVEL', 'INFO').upper()
    if json_mode is None:
        # 容器里 stdout 不是 TTY → JSON,开发 terminal → 彩色
        json_mode = not sys.stderr.isatty()

    # 共享处理链 — structlog 的 processor,处理的是 structlog 产生的 event_dict
    shared_processors = [
        merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt='iso', utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_service_fields,
        _redact_sensitive,
    ]

    if json_mode:
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    # structlog 配置 — 让 structlog 的 logger 通过 stdlib logging 输出,
    # 真正的格式化交给 stdlib 的 handler (通过 ProcessorFormatter)
    structlog.configure(
        processors=shared_processors + [
            # ProcessorFormatter.wrap_for_formatter — 把 event_dict 打包成
            # stdlib handler 能理解的格式,再由 handler 的 ProcessorFormatter 渲染
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stdlib handler 的格式化器: 用同一套 processors 渲染成最终字符串
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,   # 非 structlog 产生的日志也走这套处理
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # 额外的文件日志 (滚动,始终 JSON)
    from logging.handlers import RotatingFileHandler
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )
    file_handler = RotatingFileHandler(
        LOG_DIR / 'platform.jsonl',
        maxBytes=50 * 1024 * 1024,
        backupCount=10,
        encoding='utf-8',
    )
    file_handler.setFormatter(file_formatter)

    root = logging.getLogger()
    root.handlers = [console_handler, file_handler]
    root.setLevel(level)

    # 降噪
    for noisy in ('ccxt', 'websockets.client', 'urllib3.connectionpool',
                  'werkzeug'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.get_logger('engine.logging_setup').info(
        'logging_initialized', level=level, json_mode=json_mode
    )


def get_logger(name: str = None):
    """获取一个 structlog logger"""
    return structlog.get_logger(name)


# ============================================================
# Context helpers
# ============================================================

def bind_context(**kwargs):
    """绑定上下文到当前任务/线程,后续所有日志自动带上

    用法:
        with LogContext(strategy_id='abc', trade_id=123):
            logger.info('opened')       # 自动带 strategy_id, trade_id

    或者在请求入口:
        bind_context(request_id=uuid.uuid4().hex[:8], path=request.path)
    """
    bind_contextvars(**kwargs)


def unbind_context(*keys):
    unbind_contextvars(*keys)


def clear_context():
    clear_contextvars()


class LogContext:
    """上下文管理器形式"""
    def __init__(self, **kwargs):
        self._kwargs = kwargs

    def __enter__(self):
        bind_contextvars(**self._kwargs)
        return self

    def __exit__(self, *exc):
        unbind_contextvars(*self._kwargs.keys())
