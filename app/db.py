from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS endpoint_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                client_api_key TEXT NOT NULL UNIQUE,
                upstream_api_key TEXT NOT NULL,
                last_used_model TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                base_url TEXT NOT NULL,
                pool TEXT NOT NULL DEFAULT 'alive' CHECK (pool IN ('alive', 'revival')),
                call_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_checked_at DATETIME,
                added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(group_id, base_url),
                FOREIGN KEY(group_id) REFERENCES endpoint_groups(id) ON DELETE CASCADE
            );
            """
        )
        endpoint_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(endpoints)").fetchall()
        }
        if "added_at" not in endpoint_columns:
            conn.execute("ALTER TABLE endpoints ADD COLUMN added_at DATETIME")
            conn.execute(
                """
                UPDATE endpoints
                SET added_at = CURRENT_TIMESTAMP
                WHERE added_at IS NULL
                """
            )

        group_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(endpoint_groups)").fetchall()
        }
        if "last_used_model" not in group_columns:
            conn.execute("ALTER TABLE endpoint_groups ADD COLUMN last_used_model TEXT")
