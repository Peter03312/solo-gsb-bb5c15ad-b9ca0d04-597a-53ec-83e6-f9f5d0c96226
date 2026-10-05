"""SQLite 连接管理与初始化。

- 读：每次操作开一条短连接（WAL 下读写不互斥）。
- 写：全局锁串行化，``BEGIN IMMEDIATE`` 立即拿写锁；写操作的整个校验+插入在
  同一事务内完成，保证"整体拒绝且不改历史"的原子性。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    procedure_id  INTEGER NOT NULL REFERENCES procedures(id),
    revision      INTEGER NOT NULL,
    body          TEXT NOT NULL,
    valid_start   TEXT NOT NULL,
    valid_end     TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES versions(id),
    published_at  TEXT NOT NULL,
    UNIQUE(procedure_id, revision)
);

-- 撤回事件：只追加，不删除/改写 versions 行。
-- 一个版本至多被撤回一次（UNIQUE）；撤回时刻 withdrawn_at 由服务端记录，不可回填。
-- 双时间维语义：知悉时刻 k 下该版本可见当且仅当
-- published_at <= k 且（未撤回 或 withdrawn_at > k）。
CREATE TABLE IF NOT EXISTS withdrawals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL UNIQUE REFERENCES versions(id),
    withdrawn_at TEXT NOT NULL
);

-- 作业规程包：班组开工前按（作业时刻, 知悉时刻, 一组规程编号）固定。
-- 包一旦形成即不可变：各规程当时适用版本的正文等字段在 package_items 中
-- 完整留快照，之后任何更正/撤回都只作用于 versions/withdrawals，
-- 按包标识取回的内容永远保持固定时原样。
CREATE TABLE IF NOT EXISTS packages (
    id          TEXT PRIMARY KEY,
    work_time   TEXT NOT NULL,
    known_as_of TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS package_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id          TEXT NOT NULL REFERENCES packages(id),
    position            INTEGER NOT NULL,
    procedure_id        INTEGER NOT NULL REFERENCES procedures(id),
    number              TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    body                TEXT NOT NULL,
    valid_start         TEXT NOT NULL,
    valid_end           TEXT NOT NULL,
    supersedes_revision INTEGER,
    published_at        TEXT NOT NULL,
    UNIQUE(package_id, position)
);

CREATE INDEX IF NOT EXISTS idx_versions_procedure
    ON versions(procedure_id);
CREATE INDEX IF NOT EXISTS idx_versions_query
    ON versions(published_at, valid_start, valid_end);
CREATE INDEX IF NOT EXISTS idx_withdrawals_version
    ON withdrawals(version_id);
CREATE INDEX IF NOT EXISTS idx_package_items_package
    ON package_items(package_id);
"""

_write_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path, timeout=settings.busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(settings.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """幂等建表；内存库可安全重复调用。"""
    if settings.db_path != ":memory:":
        parent = os.path.dirname(os.path.abspath(settings.db_path))
        os.makedirs(parent, exist_ok=True)
    conn = _connect()
    try:
        if settings.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def read_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def read_snapshot() -> Iterator[sqlite3.Connection]:
    """只读一致性快照：显式开启一条 deferred 事务，并**立即执行一次读**把
    SQLite 的快照固定在进入时刻（deferred 事务的快照在首次读时取得，而非
    BEGIN 时）。此后事务内所有读取看到的都是同一数据库版本，不会拼接并发
    写入前后的结果（WAL 下读事务在该快照上保持到事务结束）。
    """
    conn = _connect()
    try:
        conn.execute("BEGIN")
        # 立即读真实表以获取 WAL 读锁、把读快照固定在进入时刻（SELECT 1 不
        # 访问任何表，不取读锁也不建立快照；deferred 事务的快照在首次表读时
        # 取得）。此后事务内所有读取都基于同一数据库版本。
        conn.execute("SELECT 1 FROM packages LIMIT 1").fetchone()
        yield conn
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """串行化的写事务：异常即回滚，绝不留下半截历史。"""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
