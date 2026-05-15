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
            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_url TEXT NOT NULL UNIQUE,
                pool TEXT NOT NULL DEFAULT 'alive' CHECK (pool IN ('alive', 'revival')),
                call_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_checked_at DATETIME,
                added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(endpoints)").fetchall()}

    if "group_id" in columns:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS endpoints_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_url TEXT NOT NULL UNIQUE,
                pool TEXT NOT NULL DEFAULT 'alive' CHECK (pool IN ('alive', 'revival')),
                call_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                last_checked_at DATETIME,
                added_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO endpoints_new
                (base_url, pool, call_count, last_error, last_checked_at, added_at, updated_at)
            SELECT base_url, pool, call_count, last_error, last_checked_at,
                   COALESCE(added_at, CURRENT_TIMESTAMP),
                   COALESCE(updated_at, CURRENT_TIMESTAMP)
            FROM endpoints;

            DROP TABLE endpoints;
            ALTER TABLE endpoints_new RENAME TO endpoints;
            DROP TABLE IF EXISTS endpoint_groups;
            """
        )
