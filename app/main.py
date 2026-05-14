from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import Settings, load_settings
from app.db import get_conn, init_db
from app.pool_manager import PoolManager
from app.schemas import AddUrlsRequest, CreateGroupRequest, GroupDetail, GroupSummary
from app.url_utils import normalize_base_url


def normalize_url(raw: str) -> str:
    return normalize_base_url(raw)


def get_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return None
    token = authorization[len(prefix) :].strip()
    return token or None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    init_db(settings.db_path)
    manager = PoolManager(settings)
    app.state.settings = settings
    app.state.pool_manager = manager

    stop_event = asyncio.Event()

    async def revival_loop() -> None:
        while not stop_event.is_set():
            await manager.revive_once()
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.revival_check_interval,
                )
            except asyncio.TimeoutError:
                pass

    tasks = [asyncio.create_task(revival_loop())]

    if settings.url_sync_file and settings.url_sync_group_id > 0:
        async def url_sync_loop() -> None:
            while not stop_event.is_set():
                manager.sync_urls_from_file(settings.url_sync_group_id, settings.url_sync_file)
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.url_sync_interval,
                    )
                except asyncio.TimeoutError:
                    pass

        tasks.append(asyncio.create_task(url_sync_loop()))
    try:
        yield
    finally:
        stop_event.set()
        await asyncio.gather(*tasks)


app = FastAPI(title="AI API Proxy", version="1.0.0", lifespan=lifespan)
app.mount("/ui", StaticFiles(directory="app/static", html=True), name="ui")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pool_manager(request: Request) -> PoolManager:
    return request.app.state.pool_manager


def require_admin(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
) -> None:
    token = get_bearer_token(authorization)
    if token != settings.admin_token:
        raise HTTPException(status_code=401, detail="invalid admin token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


@app.post("/admin/groups", dependencies=[Depends(require_admin)])
def create_group(payload: CreateGroupRequest, request: Request) -> dict[str, Any]:
    settings = get_settings(request)
    urls = [normalize_url(u) for u in payload.urls if normalize_url(u)]
    with get_conn(settings.db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO endpoint_groups(name, client_api_key, upstream_api_key)
            VALUES (?, ?, ?)
            """,
            (payload.name, payload.client_api_key, payload.upstream_api_key),
        )
        group_id = int(cur.lastrowid)
        for url in urls:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO endpoints(group_id, base_url, pool, call_count, added_at)
                VALUES (?, ?, 'alive', 0, CURRENT_TIMESTAMP)
                """,
                (group_id, url),
            )
            if cur.rowcount == 0:
                conn.execute(
                    """
                    UPDATE endpoints
                    SET pool = 'alive',
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND base_url = ?
                    """,
                    (group_id, url),
                )
    return {"group_id": group_id, "inserted_urls": len(urls)}


@app.post("/admin/groups/{group_id}/urls", dependencies=[Depends(require_admin)])
def add_urls(group_id: int, payload: AddUrlsRequest, request: Request) -> dict[str, int]:
    settings = get_settings(request)
    urls = [normalize_url(u) for u in payload.urls if normalize_url(u)]
    inserted = 0
    with get_conn(settings.db_path) as conn:
        row = conn.execute("SELECT id FROM endpoint_groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="group not found")
        for url in urls:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO endpoints(group_id, base_url, pool, call_count, added_at)
                VALUES (?, ?, 'alive', 0, CURRENT_TIMESTAMP)
                """,
                (group_id, url),
            )
            inserted += int(cur.rowcount > 0)
            if cur.rowcount == 0:
                conn.execute(
                    """
                    UPDATE endpoints
                    SET pool = 'alive',
                        last_error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE group_id = ? AND base_url = ?
                    """,
                    (group_id, url),
                )
    return {"inserted_urls": inserted}


@app.post("/admin/url-sync/run", dependencies=[Depends(require_admin)])
def run_url_sync(request: Request) -> dict[str, Any]:
    settings = get_settings(request)
    manager = get_pool_manager(request)
    if not settings.url_sync_file or settings.url_sync_group_id <= 0:
        raise HTTPException(
            status_code=400,
            detail="url sync is disabled; set URL_SYNC_FILE and URL_SYNC_GROUP_ID",
        )
    inserted = manager.sync_urls_from_file(settings.url_sync_group_id, settings.url_sync_file)
    return {
        "inserted_urls": inserted,
        "group_id": settings.url_sync_group_id,
        "file": settings.url_sync_file,
    }


@app.get("/admin/groups", response_model=list[GroupSummary], dependencies=[Depends(require_admin)])
def list_groups(request: Request) -> list[GroupSummary]:
    settings = get_settings(request)
    with get_conn(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT g.id, g.name,
                   SUM(CASE WHEN e.pool = 'alive' THEN 1 ELSE 0 END) AS alive_count,
                   SUM(CASE WHEN e.pool = 'revival' THEN 1 ELSE 0 END) AS revival_count
            FROM endpoint_groups g
            LEFT JOIN endpoints e ON e.group_id = g.id
            GROUP BY g.id, g.name
            ORDER BY g.id
            """
        ).fetchall()
    return [
        GroupSummary(
            id=row["id"],
            name=row["name"],
            alive_count=int(row["alive_count"] or 0),
            revival_count=int(row["revival_count"] or 0),
        )
        for row in rows
    ]


@app.get("/admin/groups/{group_id}", response_model=GroupDetail, dependencies=[Depends(require_admin)])
def get_group(group_id: int, request: Request) -> GroupDetail:
    settings = get_settings(request)
    with get_conn(settings.db_path) as conn:
        g = conn.execute(
            """
            SELECT id, name, client_api_key, last_used_model
            FROM endpoint_groups
            WHERE id = ?
            """,
            (group_id,),
        ).fetchone()
        if not g:
            raise HTTPException(status_code=404, detail="group not found")
        es = conn.execute(
            """
            SELECT id, base_url, pool, call_count, last_error, last_checked_at, added_at
            FROM endpoints
            WHERE group_id = ?
            ORDER BY id
            """,
            (group_id,),
        ).fetchall()
    return GroupDetail(
        id=g["id"],
        name=g["name"],
        client_api_key=g["client_api_key"],
        last_used_model=g["last_used_model"],
        endpoints=[
            {
                "id": e["id"],
                "base_url": e["base_url"],
                "pool": e["pool"],
                "call_count": e["call_count"],
                "last_error": e["last_error"],
                "last_checked_at": e["last_checked_at"],
                "added_at": e["added_at"],
            }
            for e in es
        ],
    )


@app.api_route("/v1/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_openai(
    full_path: str,
    request: Request,
    manager: PoolManager = Depends(get_pool_manager),
    authorization: str | None = Header(default=None),
):
    token = get_bearer_token(authorization)
    if not token:
        return JSONResponse(status_code=401, content={"error": "missing bearer token"})

    body = await request.body()
    model_hint: str | None = None
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("model"), str):
                model_hint = parsed["model"].strip() or None
        except (UnicodeDecodeError, json.JSONDecodeError):
            model_hint = None

    header_map = {k: v for k, v in request.headers.items()}
    query_map = {k: v for k, v in request.query_params.items()}
    status_code, content, upstream_headers = await manager.proxy_with_failover(
        client_key=token,
        method=request.method,
        path=full_path,
        body=body,
        headers=header_map,
        query_params=query_map,
        model_hint=model_hint,
    )
    response_headers = {}
    for k, v in upstream_headers.items():
        lk = k.lower()
        if lk in {"content-length", "transfer-encoding", "connection"}:
            continue
        response_headers[k] = v
    return Response(status_code=status_code, content=content, headers=response_headers)
