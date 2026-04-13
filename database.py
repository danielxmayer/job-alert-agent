"""
SQLite-backed persistence for seen jobs, scrape logs, and audit trail.
Uses WAL mode and a threading lock for safe concurrent access.
"""

import sqlite3
import threading
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _connect(db_file: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_file: str) -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _lock:
        conn = _connect(db_file)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS seen_jobs (
                    job_id       TEXT PRIMARY KEY,
                    title        TEXT,
                    company      TEXT,
                    portal       TEXT,
                    first_seen   TEXT,
                    score        INTEGER DEFAULT NULL,
                    score_reason TEXT    DEFAULT NULL
                );

                CREATE TABLE IF NOT EXISTS scrape_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id       TEXT,
                    portal       TEXT,
                    keyword      TEXT,
                    location     TEXT,
                    jobs_found   INTEGER DEFAULT 0,
                    error_count  INTEGER DEFAULT 0,
                    error_msg    TEXT,
                    started_at   TEXT,
                    duration_ms  INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id       TEXT,
                    job_id       TEXT,
                    title        TEXT,
                    portal       TEXT,
                    score        INTEGER,
                    score_reason TEXT,
                    action       TEXT,
                    created_at   TEXT
                );
            """)
            conn.commit()
            log.debug("Database initialised: %s", db_file)
        finally:
            conn.close()


def load_seen(db_file: str) -> set:
    """Return set of all known job IDs."""
    with _lock:
        conn = _connect(db_file)
        try:
            rows = conn.execute("SELECT job_id FROM seen_jobs").fetchall()
            return {r["job_id"] for r in rows}
        finally:
            conn.close()


def add_seen_job(db_file: str, job_id: str, job: dict) -> None:
    """Insert a job into seen_jobs (ignore if already present)."""
    with _lock:
        conn = _connect(db_file)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_jobs
                    (job_id, title, company, portal, first_seen, score, score_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("portal", ""),
                    datetime.now(timezone.utc).isoformat(),
                    job.get("score"),
                    job.get("score_reason", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def log_scrape(
    db_file: str,
    run_id: str,
    portal: str,
    keyword: str,
    location: str,
    jobs_found: int,
    error_count: int,
    error_msg: str | None = None,
    started_at: str | None = None,
    duration_ms: int = 0,
) -> None:
    """Record one scrape attempt in scrape_log."""
    with _lock:
        conn = _connect(db_file)
        try:
            conn.execute(
                """
                INSERT INTO scrape_log
                    (run_id, portal, keyword, location, jobs_found,
                     error_count, error_msg, started_at, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, portal, keyword, location,
                    jobs_found, error_count, error_msg,
                    started_at or datetime.now(timezone.utc).isoformat(),
                    duration_ms,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def log_audit(db_file: str, run_id: str, job_id: str, job: dict, action: str) -> None:
    """Record a scoring/notification decision in audit_log."""
    with _lock:
        conn = _connect(db_file)
        try:
            conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, job_id, title, portal, score, score_reason, action, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    job.get("title", ""),
                    job.get("portal", ""),
                    job.get("score"),
                    job.get("score_reason", ""),
                    action,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
