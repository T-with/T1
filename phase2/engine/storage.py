"""
engine/storage.py — SQLite 持久化层

替代原来的 JSON 文件存储,解决:
1. 并发写 JSON 损坏 — SQLite WAL 模式天然支持多读一写
2. 重启丢失交易历史 — 所有 trade/risk_event 落盘
3. 凯利样本丢失 — trade_results 持久化
4. 事件审计 — 可以查"那次熔断为什么"

兼容 JSON 文件 — 首次启动自动迁移。
"""

import sqlite3
import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / 'data'
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / 'platform.db'


# ============================================================
# Schema
# ============================================================

SCHEMA = """
-- 策略配置 (替代 strategies.json)
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    type TEXT NOT NULL,
    config_json TEXT NOT NULL,     -- 完整 StrategyConfig 序列化
    status TEXT DEFAULT 'stopped',
    paper INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);

-- 交易所配置 (替代 exchange.json,仍用加密)
CREATE TABLE IF NOT EXISTS exchange_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),    -- 单行
    exchange_id TEXT,
    api_key TEXT,                  -- 加密后的
    api_secret TEXT,               -- 加密后的
    passphrase TEXT,               -- 加密后的
    updated_at TEXT NOT NULL
);

-- 交易流水 (实盘+纸面)
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- long/short
    type TEXT NOT NULL,            -- signal_open/stop_loss/take_profit/trailing_stop/...
    entry_price REAL,
    exit_price REAL,
    size REAL,
    pnl REAL,
    pnl_pct REAL,
    opened_at TEXT,
    closed_at TEXT NOT NULL,
    paper INTEGER DEFAULT 1,
    exchange_order_id TEXT,
    meta_json TEXT                 -- 任意元数据 (kelly_info, confidence, etc.)
);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id, closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(closed_at DESC);

-- 持仓快照 (每次 tick 写入,用于重启对账)
CREATE TABLE IF NOT EXISTS positions (
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    highest_price REAL,
    lowest_price REAL,
    exchange_order_id TEXT,
    PRIMARY KEY (strategy_id, symbol)
);

-- 风控事件 (替代内存 deque)
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,       -- stop_loss / circuit_breaker / ...
    level TEXT NOT NULL,            -- normal/warning/danger/critical
    strategy_id TEXT,
    symbol TEXT,
    message TEXT,
    data_json TEXT,
    action_taken TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON risk_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_strategy ON risk_events(strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_level ON risk_events(level, timestamp DESC);

-- 权益曲线 (日度快照)
CREATE TABLE IF NOT EXISTS equity_snapshots (
    strategy_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    equity REAL NOT NULL,
    cash REAL,
    unrealized_pnl REAL,
    position_value REAL,
    drawdown_pct REAL,
    peak_equity REAL,
    PRIMARY KEY (strategy_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_equity_time ON equity_snapshots(timestamp DESC);

-- 系统事件审计 (启动/停止/熔断解除等人工操作)
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,           -- start_strategy / stop_strategy / release_circuit_breaker
    target TEXT,                    -- strategy_id, 'global', etc.
    user TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);

-- Schema 版本
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

CURRENT_SCHEMA_VERSION = 1


# ============================================================
# 连接管理
# ============================================================

class Database:
    """线程安全的 SQLite 封装 (WAL 模式 + 连接池)"""

    _local = threading.local()

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程一个连接,避免 'Recursive use of cursor' 问题"""
        if not hasattr(self._local, 'conn'):
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,   # 自动提交,配合 with conn 显式事务
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            # WAL 模式: 多读一写并发
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')  # WAL 下 NORMAL 足够安全
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=30000')
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def transaction(self):
        """显式事务 context manager"""
        conn = self._get_conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._get_conn().execute(sql, params)

    def executemany(self, sql: str, params_list: list) -> sqlite3.Cursor:
        return self._get_conn().executemany(sql, params_list)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple = ()) -> List[dict]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def _ensure_schema(self):
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_conn()
            conn.executescript(SCHEMA)

            row = conn.execute(
                'SELECT MAX(version) AS v FROM schema_version'
            ).fetchone()
            current = (row['v'] or 0) if row else 0
            if current < CURRENT_SCHEMA_VERSION:
                conn.execute(
                    'INSERT OR REPLACE INTO schema_version(version, applied_at) VALUES(?, ?)',
                    (CURRENT_SCHEMA_VERSION, datetime.now().isoformat()),
                )
            self._initialized = True
            logger.info(f"Database initialized at {self.db_path} (schema v{CURRENT_SCHEMA_VERSION})")


# ============================================================
# Repository 层 — 对业务代码暴露的接口
# ============================================================

class StrategyRepo:
    """策略配置 CRUD"""

    def __init__(self, db: Database):
        self.db = db

    def list_all(self) -> Dict[str, dict]:
        rows = self.db.fetchall("SELECT id, config_json FROM strategies ORDER BY created_at DESC")
        return {r['id']: json.loads(r['config_json']) for r in rows}

    def get(self, sid: str) -> Optional[dict]:
        row = self.db.fetchone("SELECT config_json FROM strategies WHERE id = ?", (sid,))
        return json.loads(row['config_json']) if row else None

    def upsert(self, sid: str, config: dict):
        now = datetime.now().isoformat()
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO strategies(id, name, symbol, timeframe, type,
                                       config_json, status, paper,
                                       created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    symbol=excluded.symbol,
                    timeframe=excluded.timeframe,
                    type=excluded.type,
                    config_json=excluded.config_json,
                    status=excluded.status,
                    paper=excluded.paper,
                    updated_at=excluded.updated_at
            """, (
                sid,
                config.get('name', ''),
                config.get('symbol', ''),
                config.get('timeframe', ''),
                config.get('type', ''),
                json.dumps(config, ensure_ascii=False),
                config.get('status', 'stopped'),
                1 if config.get('paper', True) else 0,
                config.get('created_at', now),
                now,
            ))

    def update_status(self, sid: str, status: str):
        now = datetime.now().isoformat()
        with self.db.transaction() as conn:
            # 同时更新 config_json 里的 status (保持一致)
            row = conn.execute(
                "SELECT config_json FROM strategies WHERE id=?", (sid,)
            ).fetchone()
            if not row:
                return
            cfg = json.loads(row['config_json'])
            cfg['status'] = status
            cfg['updated_at'] = now
            conn.execute(
                "UPDATE strategies SET status=?, config_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(cfg, ensure_ascii=False), now, sid),
            )

    def delete(self, sid: str):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM strategies WHERE id=?", (sid,))


class ExchangeConfigRepo:
    """交易所配置 (单行)"""

    def __init__(self, db: Database):
        self.db = db

    def get(self) -> dict:
        row = self.db.fetchone("SELECT * FROM exchange_config WHERE id=1")
        if not row:
            return {}
        return {
            'exchange_id': row.get('exchange_id', ''),
            'api_key': row.get('api_key', ''),
            'api_secret': row.get('api_secret', ''),
            'passphrase': row.get('passphrase', ''),
        }

    def save(self, data: dict):
        now = datetime.now().isoformat()
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO exchange_config(id, exchange_id, api_key, api_secret,
                                            passphrase, updated_at)
                VALUES(1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    exchange_id=excluded.exchange_id,
                    api_key=excluded.api_key,
                    api_secret=excluded.api_secret,
                    passphrase=excluded.passphrase,
                    updated_at=excluded.updated_at
            """, (
                data.get('exchange_id', ''),
                data.get('api_key', ''),
                data.get('api_secret', ''),
                data.get('passphrase', ''),
                now,
            ))


class TradeRepo:
    """交易流水"""

    def __init__(self, db: Database):
        self.db = db

    def insert(self, trade: dict) -> int:
        with self.db.transaction() as conn:
            cur = conn.execute("""
                INSERT INTO trades(strategy_id, symbol, side, type,
                                   entry_price, exit_price, size,
                                   pnl, pnl_pct, opened_at, closed_at,
                                   paper, exchange_order_id, meta_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get('strategy_id', ''),
                trade.get('symbol', ''),
                trade.get('side', ''),
                trade.get('type', ''),
                trade.get('entry_price'),
                trade.get('exit_price'),
                trade.get('size'),
                trade.get('pnl'),
                trade.get('pnl_pct'),
                trade.get('opened_at'),
                trade.get('closed_at', datetime.now().isoformat()),
                1 if trade.get('paper', True) else 0,
                trade.get('exchange_order_id'),
                json.dumps(trade.get('meta', {}), ensure_ascii=False),
            ))
            return cur.lastrowid

    def recent_for_strategy(self, sid: str, limit: int = 50) -> List[dict]:
        return self.db.fetchall(
            "SELECT * FROM trades WHERE strategy_id=? ORDER BY closed_at DESC LIMIT ?",
            (sid, limit),
        )

    def recent_all(self, limit: int = 100) -> List[dict]:
        return self.db.fetchall(
            "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,),
        )

    def stats_for_strategy(self, sid: str) -> dict:
        """聚合统计,供凯利公式使用"""
        row = self.db.fetchone("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                AVG(CASE WHEN pnl > 0 THEN pnl_pct END) AS avg_win,
                AVG(CASE WHEN pnl <= 0 THEN pnl_pct END) AS avg_loss
            FROM trades
            WHERE strategy_id=? AND pnl IS NOT NULL
        """, (sid,))
        return row or {'total': 0, 'wins': 0, 'losses': 0, 'avg_win': 0, 'avg_loss': 0}

    def pnl_for_period(self, sid: str = None, hours: int = 24) -> float:
        """指定窗口内的累计 PnL"""
        cutoff = datetime.fromtimestamp(time.time() - hours * 3600).isoformat()
        if sid:
            row = self.db.fetchone(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE strategy_id=? AND closed_at >= ?",
                (sid, cutoff),
            )
        else:
            row = self.db.fetchone(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE closed_at >= ?",
                (cutoff,),
            )
        return row['total'] if row else 0.0


class PositionRepo:
    """持仓快照 — 用于重启对账"""

    def __init__(self, db: Database):
        self.db = db

    def upsert(self, strategy_id: str, symbol: str, pos: dict):
        now = datetime.now().isoformat()
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO positions(strategy_id, symbol, side, size,
                                      entry_price, current_price, opened_at,
                                      updated_at, highest_price, lowest_price,
                                      exchange_order_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(strategy_id, symbol) DO UPDATE SET
                    side=excluded.side,
                    size=excluded.size,
                    entry_price=excluded.entry_price,
                    current_price=excluded.current_price,
                    updated_at=excluded.updated_at,
                    highest_price=MAX(positions.highest_price, excluded.current_price),
                    lowest_price=MIN(positions.lowest_price, excluded.current_price)
            """, (
                strategy_id, symbol,
                pos.get('side', ''),
                pos.get('size', 0),
                pos.get('entry_price', 0),
                pos.get('current_price', 0),
                pos.get('opened_at', now),
                now,
                pos.get('highest_price', pos.get('current_price', 0)),
                pos.get('lowest_price', pos.get('current_price', 0)),
                pos.get('exchange_order_id'),
            ))

    def delete(self, strategy_id: str, symbol: str):
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM positions WHERE strategy_id=? AND symbol=?",
                (strategy_id, symbol),
            )

    def for_strategy(self, strategy_id: str) -> Dict[str, dict]:
        rows = self.db.fetchall(
            "SELECT * FROM positions WHERE strategy_id=?", (strategy_id,)
        )
        return {r['symbol']: r for r in rows}

    def all(self) -> List[dict]:
        return self.db.fetchall("SELECT * FROM positions")


class RiskEventRepo:
    """风控事件"""

    def __init__(self, db: Database):
        self.db = db

    def log(self, event: dict):
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO risk_events(timestamp, event_type, level,
                                        strategy_id, symbol, message,
                                        data_json, action_taken, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                event.get('timestamp', time.time()),
                event.get('event_type', ''),
                event.get('level', 'normal'),
                event.get('strategy_id'),
                event.get('symbol'),
                event.get('message', ''),
                json.dumps(event.get('data', {}), ensure_ascii=False),
                event.get('action_taken', ''),
                datetime.now().isoformat(),
            ))

    def recent(self, limit: int = 100, level: str = None) -> List[dict]:
        if level:
            rows = self.db.fetchall(
                "SELECT * FROM risk_events WHERE level=? ORDER BY timestamp DESC LIMIT ?",
                (level, limit),
            )
        else:
            rows = self.db.fetchall(
                "SELECT * FROM risk_events ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
        for r in rows:
            try:
                r['data'] = json.loads(r.get('data_json') or '{}')
            except Exception:
                r['data'] = {}
        return rows

    def count_by_type(self, hours: int = 24) -> Dict[str, int]:
        cutoff = time.time() - hours * 3600
        rows = self.db.fetchall("""
            SELECT event_type, COUNT(*) AS c FROM risk_events
            WHERE timestamp >= ? GROUP BY event_type
        """, (cutoff,))
        return {r['event_type']: r['c'] for r in rows}


class AuditRepo:
    """审计日志 — 人工操作记录"""

    def __init__(self, db: Database):
        self.db = db

    def log(self, action: str, target: str = None, user: str = 'system',
            details: dict = None):
        with self.db.transaction() as conn:
            conn.execute("""
                INSERT INTO audit_log(timestamp, action, target, user,
                                      details_json, created_at)
                VALUES(?,?,?,?,?,?)
            """, (
                time.time(), action, target, user,
                json.dumps(details or {}, ensure_ascii=False),
                datetime.now().isoformat(),
            ))

    def recent(self, limit: int = 100) -> List[dict]:
        return self.db.fetchall(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        )


# ============================================================
# JSON → SQLite 迁移 (首次启动自动跑)
# ============================================================

def migrate_from_json(db: Database):
    """从旧的 strategies.json / exchange.json 迁移到 SQLite"""
    strategies_file = DATA_DIR / 'strategies.json'
    exchange_file = DATA_DIR / 'exchange.json'

    migrated = 0

    # 检查是否已经迁移过 (通过数据库里有没有记录)
    row = db.fetchone("SELECT COUNT(*) AS c FROM strategies")
    if row and row['c'] > 0:
        logger.info("SQLite already has strategies, skipping migration")
        return 0

    # 迁移策略
    if strategies_file.exists():
        try:
            data = json.loads(strategies_file.read_text(encoding='utf-8'))
            repo = StrategyRepo(db)
            for sid, config in data.items():
                # 清理可能泄露的密钥字段 — 这些现在从 exchange_config 读取
                cleaned = {k: v for k, v in config.items()
                          if k not in ('api_key', 'api_secret', 'passphrase')}
                repo.upsert(sid, cleaned)
                migrated += 1
            # 重命名为 .migrated 避免重复迁移
            strategies_file.rename(strategies_file.with_suffix('.json.migrated'))
            logger.info(f"Migrated {migrated} strategies from JSON → SQLite")
        except Exception as e:
            logger.error(f"Strategy migration failed: {e}")

    # 迁移交易所配置
    if exchange_file.exists():
        try:
            data = json.loads(exchange_file.read_text(encoding='utf-8'))
            repo = ExchangeConfigRepo(db)
            repo.save(data)
            exchange_file.rename(exchange_file.with_suffix('.json.migrated'))
            logger.info("Migrated exchange config from JSON → SQLite")
        except Exception as e:
            logger.error(f"Exchange config migration failed: {e}")

    return migrated


# ============================================================
# 全局单例
# ============================================================

db = Database()
strategy_repo = StrategyRepo(db)
exchange_repo = ExchangeConfigRepo(db)
trade_repo = TradeRepo(db)
position_repo = PositionRepo(db)
risk_event_repo = RiskEventRepo(db)
audit_repo = AuditRepo(db)

# 启动时迁移
migrate_from_json(db)
