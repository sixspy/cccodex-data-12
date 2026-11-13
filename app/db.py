import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        # isolation_level=None：连接置于 autocommit，事务边界完全由我们显式控制，
        # 这样 db_transaction() 可以发 BEGIN IMMEDIATE，把并发“继续”串行化。
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,
            timeout=30,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 30000")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    """显式立即事务：并发写入在第一时间串行化，避免两边都写进账册造成超池。"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def _column_names(db, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(db, table: str, column: str, decl: str):
    if column not in _column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(app):
    with app.app_context():
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS crawl_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    seed_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    weight INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS crawl_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                    item_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    item_code TEXT,
                    pool_day TEXT,
                    admitted_at TEXT,
                    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(job_id, item_key)
                );
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_pool (
                    pool_day TEXT PRIMARY KEY,
                    capacity INTEGER NOT NULL,
                    allocation_policy TEXT NOT NULL,
                    used_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS pool_waiting (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pool_day TEXT NOT NULL,
                    job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                    item_code TEXT NOT NULL,
                    item_key TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    seq INTEGER NOT NULL,
                    UNIQUE(pool_day, job_id, item_code)
                );
                CREATE TABLE IF NOT EXISTS pool_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_day TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    job_id INTEGER,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS pool_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    job_id INTEGER NOT NULL,
                    lock_day TEXT NOT NULL,
                    locked_at TEXT NOT NULL
                );
                """
            )
            # 旧快照库补列（基线表没有 weight / item_code / pool_day / admitted_at）
            _ensure_column(db, "crawl_jobs", "weight", "INTEGER NOT NULL DEFAULT 1")
            _ensure_column(db, "crawl_records", "item_code", "TEXT")
            _ensure_column(db, "crawl_records", "pool_day", "TEXT")
            _ensure_column(db, "crawl_records", "admitted_at", "TEXT")
            _ensure_column(db, "pool_waiting", "item_key", "TEXT NOT NULL DEFAULT ''")
            db.commit()
        except Exception:
            db.rollback()
            raise
