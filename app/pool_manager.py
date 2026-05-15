from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import httpx

from app.config import Settings
from app.db import get_conn
from app.url_utils import normalize_base_url


class PoolManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()

    def upsert_endpoint_alive(self, base_url: str) -> bool:
        normalized = normalize_base_url(base_url)
        if not normalized:
            return False
        with get_conn(self.settings.db_path) as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO endpoints(base_url, pool, call_count, added_at)
                VALUES (?, 'alive', 0, CURRENT_TIMESTAMP)
                """,
                (normalized,),
            ).rowcount > 0
            conn.execute(
                """
                UPDATE endpoints
                SET pool = 'alive', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE base_url = ?
                """,
                (normalized,),
            )
            return inserted

    def sync_urls_from_file(self, file_path: str) -> int:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return 0
        inserted = 0
        processed = 0
        for raw in lines:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            processed += 1
            if self.upsert_endpoint_alive(value):
                inserted += 1
        if processed > 0:
            path.write_text("", encoding="utf-8")
        return inserted

    async def _health_check(self, base_url: str) -> tuple[bool, str | None]:
        probe_url = f"{base_url.rstrip('/')}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.upstream_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.default_model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        timeout = httpx.Timeout(self.settings.health_check_timeout)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(probe_url, headers=headers, json=payload)
            if resp.status_code >= 400:
                return False, f"health check status {resp.status_code}"
            if not resp.text.strip():
                return False, "health check empty response"
            return True, None
        except Exception as exc:
            return False, f"health check exception: {exc}"

    def _move_to_revival(self, endpoint_id: int, reason: str) -> None:
        with get_conn(self.settings.db_path) as conn:
            conn.execute(
                """
                UPDATE endpoints
                SET pool = 'revival', last_error = ?, last_checked_at = ?,
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
                SET call_count = call_count + 1, last_error = NULL,
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
                SET call_count = 0, last_error = NULL, last_checked_at = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), endpoint_id),
            )

    def _list_alive_endpoints(self, excluded: set[int]) -> list[sqlite3.Row]:
        with get_conn(self.settings.db_path) as conn:
            rows = conn.execute(
                "SELECT id, base_url, call_count FROM endpoints WHERE pool = 'alive'"
            ).fetchall()
            return [r for r in rows if r["id"] not in excluded]

    async def _select_endpoint(self, excluded: set[int]) -> sqlite3.Row | None:
        async with self._lock:
            alive = self._list_alive_endpoints(excluded)
            if not alive:
                return None
            random.shuffle(alive)
            for row in alive:
                if row["call_count"] < self.settings.max_calls_before_check:
                    return row
            candidate = alive[0]

        ok, reason = await self._health_check(candidate["base_url"])
        if ok:
            self._reset_count(candidate["id"])
            with get_conn(self.settings.db_path) as conn:
                return conn.execute(
                    "SELECT id, base_url, call_count FROM endpoints WHERE id = ?",
                    (candidate["id"],),
                ).fetchone()
        else:
            self._move_to_revival(candidate["id"], reason or "health check failed")
            return await self._select_endpoint(excluded | {candidate["id"]})

    def _build_req_headers(self, incoming: dict[str, str]) -> dict[str, str]:
        req_headers: dict[str, str] = {}
        for k, v in incoming.items():
            if k.lower() in {"authorization", "host", "content-length"}:
                continue
            req_headers[k] = v
        req_headers["Authorization"] = f"Bearer {self.settings.upstream_api_key}"
        return req_headers

    async def proxy_with_failover(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        query_params: dict[str, str],
    ) -> tuple[int, bytes, dict[str, str]]:
        tried: set[int] = set()
        while True:
            endpoint = await self._select_endpoint(tried)
            if not endpoint:
                return 503, b'{"error":"no healthy upstream endpoint"}', {"content-type": "application/json"}

            tried.add(endpoint["id"])
            target_url = f"{endpoint['base_url'].rstrip('/')}/v1/{path}"
            req_headers = self._build_req_headers(headers)
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
                if upstream_resp.status_code >= 500:
                    self._move_to_revival(endpoint["id"], f"upstream server error {upstream_resp.status_code}")
                    continue
                if not upstream_resp.content:
                    self._move_to_revival(endpoint["id"], "empty upstream response")
                    continue
                self._mark_success(endpoint["id"])
                return upstream_resp.status_code, upstream_resp.content, dict(upstream_resp.headers)
            except Exception as exc:
                self._move_to_revival(endpoint["id"], f"request exception: {exc}")
                continue

    async def proxy_with_failover_stream(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        query_params: dict[str, str],
    ) -> tuple[int, AsyncIterator[bytes], dict[str, str]]:
        tried: set[int] = set()
        while True:
            endpoint = await self._select_endpoint(tried)
            if not endpoint:
                async def _err() -> AsyncIterator[bytes]:
                    yield b'{"error":"no healthy upstream endpoint"}'
                return 503, _err(), {"content-type": "application/json"}

            tried.add(endpoint["id"])
            target_url = f"{endpoint['base_url'].rstrip('/')}/v1/{path}"
            req_headers = self._build_req_headers(headers)
            timeout = httpx.Timeout(self.settings.request_timeout)
            client = httpx.AsyncClient(timeout=timeout)
            try:
                req = client.build_request(method, target_url, headers=req_headers, content=body, params=query_params)
                resp = await client.send(req, stream=True)

                if resp.status_code >= 500:
                    await resp.aclose()
                    await client.aclose()
                    self._move_to_revival(endpoint["id"], f"upstream server error {resp.status_code}")
                    continue

                first_chunk: bytes | None = None
                try:
                    async for raw in resp.aiter_bytes():
                        if raw:
                            first_chunk = raw
                            break
                except Exception as exc:
                    await resp.aclose()
                    await client.aclose()
                    self._move_to_revival(endpoint["id"], f"stream read error: {exc}")
                    continue

                if not first_chunk:
                    await resp.aclose()
                    await client.aclose()
                    self._move_to_revival(endpoint["id"], "empty streaming response")
                    continue

                self._mark_success(endpoint["id"])
                resp_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in {"content-length", "transfer-encoding", "connection"}
                }
                saved_first = first_chunk

                async def _stream(
                    r: httpx.Response = resp,
                    c: httpx.AsyncClient = client,
                    first: bytes = saved_first,
                ) -> AsyncIterator[bytes]:
                    try:
                        yield first
                        async for chunk in r.aiter_bytes():
                            if chunk:
                                yield chunk
                    finally:
                        await r.aclose()
                        await c.aclose()

                return resp.status_code, _stream(), resp_headers

            except Exception as exc:
                await client.aclose()
                self._move_to_revival(endpoint["id"], f"request exception: {exc}")
                continue

    async def revive_once(self) -> None:
        with get_conn(self.settings.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, base_url
                FROM endpoints
                WHERE pool = 'revival'
                  AND added_at IS NOT NULL
                  AND datetime('now') >= datetime(added_at, '+31 days')
                """
            ).fetchall()

        for row in rows:
            ok, reason = await self._health_check(row["base_url"])
            now = datetime.now(timezone.utc).isoformat()
            with get_conn(self.settings.db_path) as conn:
                if ok:
                    conn.execute(
                        """
                        UPDATE endpoints
                        SET pool = 'alive', call_count = 0, last_error = NULL,
                            last_checked_at = ?, added_at = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (now, now, row["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE endpoints
                        SET last_error = ?, last_checked_at = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (reason or "revival health check failed", now, row["id"]),
                    )
