from __future__ import annotations

import asyncio
import random
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.db import get_conn
from app.url_utils import normalize_base_url


@dataclass
class GroupAuth:
    id: int
    upstream_api_key: str
    last_used_model: str | None


class PoolManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _get_group_by_client_key(self, client_key: str) -> GroupAuth | None:
        with get_conn(self.settings.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, upstream_api_key, last_used_model
                FROM endpoint_groups
                WHERE client_api_key = ?
                """,
                (client_key,),
            ).fetchone()
            if not row:
                return None
            return GroupAuth(
                id=row["id"],
                upstream_api_key=row["upstream_api_key"],
                last_used_model=row["last_used_model"],
            )

    def get_group_auth(self, client_key: str) -> GroupAuth | None:
        return self._get_group_by_client_key(client_key)

    def set_last_used_model(self, group_id: int, model: str) -> None:
        with get_conn(self.settings.db_path) as conn:
            conn.execute(
                """
                UPDATE endpoint_groups
                SET last_used_model = ?
                WHERE id = ?
                """,
                (model, group_id),
            )

    def upsert_endpoint_alive(self, group_id: int, base_url: str) -> bool:
        normalized = normalize_base_url(base_url)
        if not normalized:
            return False

        with get_conn(self.settings.db_path) as conn:
            group = conn.execute(
                "SELECT id FROM endpoint_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
            if not group:
                return False

            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO endpoints(group_id, base_url, pool, call_count, added_at)
                VALUES (?, ?, 'alive', 0, CURRENT_TIMESTAMP)
                """,
                (group_id, normalized),
            ).rowcount > 0

            conn.execute(
                """
                UPDATE endpoints
                SET pool = 'alive',
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE group_id = ? AND base_url = ?
                """,
                (group_id, normalized),
            )
            return inserted

    def sync_urls_from_file(self, group_id: int, file_path: str) -> int:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return 0

        inserted = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return 0
        processed_count = 0
        for raw in lines:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            processed_count += 1
            if self.upsert_endpoint_alive(group_id, value):
                inserted += 1
        if processed_count > 0:
            path.write_text("", encoding="utf-8")
        return inserted

    async def _health_check(
        self,
        base_url: str,
        upstream_api_key: str,
        model_hint: str | None,
    ) -> tuple[bool, str | None]:
        probe_url = f"{base_url.rstrip('/')}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {upstream_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_hint or self.settings.default_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        timeout = httpx.Timeout(self.settings.health_check_timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(probe_url, headers=headers, json=payload)
            body = resp.text.strip()
            if resp.status_code >= 400:
                return False, f"health check status {resp.status_code}"
            if not body:
                return False, "health check empty response"
            return True, None
        except Exception as exc:
            return False, f"health check exception: {exc}"

    def _move_to_revival(self, endpoint_id: int, reason: str) -> None:
        with get_conn(self.settings.db_path) as conn:
            conn.execute(
                """
                UPDATE endpoints
                SET pool = 'revival',
                    last_error = ?,
                    last_checked_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reason, datetime.now(timezone.utc).isoformat(), endpoint_id),
            )

    def _mark_success(self, endpoint_id: int) -> None:
        with get_conn(self.settings.db_path) as conn:
            conn.execute(
                """
                UPDATE endpoints
                SET call_count = call_count + 1,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (endpoint_id,),
            )

    def _reset_count(self, endpoint_id: int) -> None:
        with get_conn(self.settings.db_path) as conn:
            conn.execute(
                """
                UPDATE endpoints
                SET call_count = 0,
                    last_error = NULL,
                    last_checked_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), endpoint_id),
            )

    def _list_alive_endpoints(self, group_id: int, excluded: set[int]) -> list[sqlite3.Row]:
        with get_conn(self.settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, base_url, call_count
                FROM endpoints
                WHERE group_id = ? AND pool = 'alive'
                """,
                (group_id,),
            ).fetchall()
            return [r for r in rows if r["id"] not in excluded]

    async def _select_endpoint(
        self,
        group_id: int,
        upstream_api_key: str,
        model_hint: str | None,
        excluded: set[int],
    ) -> sqlite3.Row | None:
        lock = self._locks[group_id]
        async with lock:
            alive_rows = self._list_alive_endpoints(group_id, excluded)
            if not alive_rows:
                return None

            random.shuffle(alive_rows)
            for row in alive_rows:
                if row["call_count"] < self.settings.max_calls_before_check:
                    return row

                ok, reason = await self._health_check(
                    row["base_url"],
                    upstream_api_key,
                    model_hint,
                )
                if ok:
                    self._reset_count(row["id"])
                    with get_conn(self.settings.db_path) as conn:
                        refreshed = conn.execute(
                            """
                            SELECT id, base_url, call_count
                            FROM endpoints
                            WHERE id = ?
                            """,
                            (row["id"],),
                        ).fetchone()
                    return refreshed

                self._move_to_revival(row["id"], reason or "health check failed")

            return None

    async def proxy_with_failover(
        self,
        client_key: str,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        query_params: dict[str, str],
        model_hint: str | None,
    ) -> tuple[int, bytes, dict[str, str]]:
        group = self.get_group_auth(client_key)
        if not group:
            return 401, b'{"error":"invalid proxy api key"}', {"content-type": "application/json"}

        if model_hint:
            self.set_last_used_model(group.id, model_hint)
            group.last_used_model = model_hint

        tried: set[int] = set()
        while True:
            endpoint = await self._select_endpoint(
                group_id=group.id,
                upstream_api_key=group.upstream_api_key,
                model_hint=group.last_used_model,
                excluded=tried,
            )
            if not endpoint:
                return 503, b'{"error":"no healthy upstream endpoint"}', {"content-type": "application/json"}

            tried.add(endpoint["id"])
            target_url = f"{endpoint['base_url'].rstrip('/')}/v1/{path}"
            req_headers: dict[str, str] = {}
            for k, v in headers.items():
                lk = k.lower()
                if lk in {"authorization", "host", "content-length"}:
                    continue
                req_headers[k] = v
            req_headers["Authorization"] = f"Bearer {group.upstream_api_key}"

            timeout = httpx.Timeout(self.settings.request_timeout)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    upstream_resp = await client.request(
                        method=method,
                        url=target_url,
                        headers=req_headers,
                        content=body,
                        params=query_params,
                    )
                raw_content = upstream_resp.content
                if upstream_resp.status_code >= 500:
                    self._move_to_revival(
                        endpoint["id"], f"upstream server error {upstream_resp.status_code}"
                    )
                    continue
                if not raw_content:
                    self._move_to_revival(endpoint["id"], "empty upstream response")
                    continue

                self._mark_success(endpoint["id"])
                return upstream_resp.status_code, raw_content, dict(upstream_resp.headers)
            except Exception as exc:
                self._move_to_revival(endpoint["id"], f"request exception: {exc}")
                continue

    async def revive_once(self) -> None:
        with get_conn(self.settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT e.id, e.base_url, g.upstream_api_key, g.last_used_model
                FROM endpoints e
                JOIN endpoint_groups g ON g.id = e.group_id
                WHERE e.pool = 'revival'
                  AND datetime('now') >= datetime(COALESCE(e.added_at, e.updated_at), '+31 days')
                """
            ).fetchall()

        for row in rows:
            ok, reason = await self._health_check(
                row["base_url"],
                row["upstream_api_key"],
                row["last_used_model"],
            )
            with get_conn(self.settings.db_path) as conn:
                if ok:
                    conn.execute(
                        """
                        UPDATE endpoints
                        SET pool = 'alive',
                            call_count = 0,
                            last_error = NULL,
                            last_checked_at = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (datetime.now(timezone.utc).isoformat(), row["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE endpoints
                        SET last_error = ?,
                            last_checked_at = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            reason or "revival health check failed",
                            datetime.now(timezone.utc).isoformat(),
                            row["id"],
                        ),
                    )
