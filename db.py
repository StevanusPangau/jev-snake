"""Database module for persistent token usage tracking in SQLite.

Provides atomic counters for input/output tokens and request counts,
ensuring server lifetime telemetry survives process restarts and reboots.
"""
from __future__ import annotations

import os
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "usage.db"))

# Optional baseline seeds: lifetime telemetry recorded before this snapshot was
# published. Defaults to zero for fresh open-source deployments; operators can
# preserve prior counters by setting these env vars in .env.
INITIAL_INPUT_TOKENS = int(os.environ.get("JEV_SEED_INPUT_TOKENS", "0"))
INITIAL_OUTPUT_TOKENS = int(os.environ.get("JEV_SEED_OUTPUT_TOKENS", "0"))
INITIAL_REQUESTS = int(os.environ.get("JEV_SEED_REQUESTS", "0"))


def get_connection() -> sqlite3.Connection:
    """Create a thread-safe SQLite connection with WAL mode enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    """Initialize database tables and seed baseline totals if empty."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_summary (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                requests INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
        """)
        cur = conn.execute("SELECT id FROM usage_summary WHERE id = 1;")
        if not cur.fetchone():
            conn.execute(
                """
                INSERT INTO usage_summary (id, input_tokens, output_tokens, requests, updated_at)
                VALUES (1, ?, ?, ?, ?);
                """,
                (INITIAL_INPUT_TOKENS, INITIAL_OUTPUT_TOKENS, INITIAL_REQUESTS, time.time()),
            )


def record_usage(input_tokens: int, output_tokens: int, is_jev_request: bool = True) -> tuple[int, int, int]:
    """Atomically increment token usage and return the updated cumulative totals."""
    now = time.time()
    req_inc = 1 if is_jev_request else 0
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE usage_summary
            SET input_tokens = input_tokens + ?,
                output_tokens = output_tokens + ?,
                requests = requests + ?,
                updated_at = ?
            WHERE id = 1;
            """,
            (input_tokens, output_tokens, req_inc, now),
        )
        row = conn.execute("SELECT input_tokens, output_tokens, requests FROM usage_summary WHERE id = 1;").fetchone()
        if row:
            return int(row["input_tokens"]), int(row["output_tokens"]), int(row["requests"])
        return 0, 0, 0


def get_usage() -> tuple[int, int, int]:
    """Retrieve cumulative token counts and request totals."""
    with get_connection() as conn:
        row = conn.execute("SELECT input_tokens, output_tokens, requests FROM usage_summary WHERE id = 1;").fetchone()
        if row:
            return int(row["input_tokens"]), int(row["output_tokens"]), int(row["requests"])
        return INITIAL_INPUT_TOKENS, INITIAL_OUTPUT_TOKENS, INITIAL_REQUESTS
