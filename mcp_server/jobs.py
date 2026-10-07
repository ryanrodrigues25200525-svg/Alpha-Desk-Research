"""Job registry for the MCP server: what was submitted, and how far it got."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

STATES = ("queued", "running", "done", "failed")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_db_path: Path | None = None


def _results_dir() -> Path:
    return Path(os.environ.get("TRADINGAGENTS_RESULTS_DIR") or os.path.expanduser("~/.tradingagents/logs"))


def registry_db(results_dir: str | os.PathLike | None = None) -> Path:
    """Where the job registry lives: ``<results_dir>/mcp_jobs.sqlite3``."""
    return Path(results_dir or _results_dir()) / "mcp_jobs.sqlite3"


def configure(results_dir: str | os.PathLike | None = None) -> Path:
    """Point the registry at ``results_dir`` (tests use a tmp dir)."""
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        _db_path = registry_db(results_dir)
        return _db_path


def _connect() -> sqlite3.Connection:
    global _conn, _db_path
    with _lock:
        if _conn is None:
            if _db_path is None:
                _db_path = registry_db()
            _db_path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(_db_path), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    analysts TEXT NOT NULL,
                    depth_preset TEXT NOT NULL,
                    max_debate_rounds INTEGER NOT NULL DEFAULT 1,
                    max_risk_discuss_rounds INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    progress TEXT NOT NULL,
                    report_dir TEXT,
                    error TEXT
                )"""
            )
            # Migrate registries created before the debate-bypass columns existed.
            existing = {row["name"] for row in _conn.execute("PRAGMA table_info(jobs)")}
            if "max_debate_rounds" not in existing:
                _conn.execute("ALTER TABLE jobs ADD COLUMN max_debate_rounds INTEGER NOT NULL DEFAULT 1")
            if "max_risk_discuss_rounds" not in existing:
                _conn.execute("ALTER TABLE jobs ADD COLUMN max_risk_discuss_rounds INTEGER NOT NULL DEFAULT 1")
            _conn.commit()
        return _conn


def _row_to_job(row: sqlite3.Row) -> dict:
    keys = set(row.keys())
    return {
        "job_id": row["job_id"],
        "ticker": row["ticker"],
        "trade_date": row["trade_date"],
        "analysts": json.loads(row["analysts"]),
        "depth_preset": row["depth_preset"],
        "max_debate_rounds": row["max_debate_rounds"] if "max_debate_rounds" in keys else 1,
        "max_risk_discuss_rounds": (
            row["max_risk_discuss_rounds"] if "max_risk_discuss_rounds" in keys else 1
        ),
        "state": row["state"],
        "progress": json.loads(row["progress"]),
        "report_dir": row["report_dir"],
        "error": row["error"],
    }


def create_job(
    *,
    ticker: str,
    trade_date: str,
    analysts: list[str],
    depth_preset: str,
    max_debate_rounds: int = 1,
    max_risk_discuss_rounds: int = 1,
) -> str:
    """Insert a ``queued`` job row; return its id."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    now_empty: dict = {}
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO jobs (job_id, ticker, trade_date, analysts, depth_preset,"
            " max_debate_rounds, max_risk_discuss_rounds, state, progress)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
            (
                job_id,
                ticker,
                trade_date,
                json.dumps(list(analysts)),
                depth_preset,
                max_debate_rounds,
                max_risk_discuss_rounds,
                json.dumps(now_empty),
            ),
        )
        conn.commit()
    return job_id


def get_job(job_id: str) -> dict | None:
    """The job row as a dict, or ``None`` when unknown."""
    with _lock:
        row = _connect().execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row is not None else None


def set_state(job_id: str, state: str, *, error: str | None = None, report_dir: str | None = None) -> None:
    """Move a job to ``state``; optionally record its failure or report dir."""
    if state not in STATES:
        raise ValueError(f"unknown job state: {state!r}; choose from {', '.join(STATES)}")
    with _lock:
        conn = _connect()
        if error is not None:
            conn.execute("UPDATE jobs SET state = ?, error = ? WHERE job_id = ?", (state, error, job_id))
        elif report_dir is not None:
            conn.execute(
                "UPDATE jobs SET state = ?, report_dir = ? WHERE job_id = ?", (state, report_dir, job_id)
            )
        else:
            conn.execute("UPDATE jobs SET state = ? WHERE job_id = ?", (state, job_id))
        conn.commit()


def set_progress(job_id: str, section: str, content: str) -> None:
    """Record one completed section's markdown while the job is running."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT progress FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        progress = json.loads(row["progress"]) if row is not None else {}
        progress[section] = content
        conn.execute("UPDATE jobs SET progress = ? WHERE job_id = ?", (json.dumps(progress), job_id))
        conn.commit()


def requeue_running() -> list[str]:
    """Back to ``queued`` every job a crashed worker left ``running``; return their ids."""
    with _lock:
        conn = _connect()
        rows = conn.execute("SELECT job_id FROM jobs WHERE state = 'running'").fetchall()
        for row in rows:
            conn.execute("UPDATE jobs SET state = 'queued' WHERE job_id = ?", (row["job_id"],))
        conn.commit()
    return [row["job_id"] for row in rows]


def list_jobs(ticker: str | None = None) -> list[dict]:
    """Newest-first job rows, optionally filtered to one ticker."""
    with _lock:
        conn = _connect()
        if ticker is not None:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE ticker = ? ORDER BY rowid DESC", (ticker,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY rowid DESC").fetchall()
    return [_row_to_job(row) for row in rows]
