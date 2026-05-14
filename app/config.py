from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    admin_token: str
    request_timeout: float
    health_check_timeout: float
    revival_check_interval: float
    url_sync_interval: float
    url_sync_file: str
    url_sync_group_id: int
    max_calls_before_check: int
    default_model: str


def load_settings() -> Settings:
    return Settings(
        db_path=os.getenv("DB_PATH", "./data/proxy.db"),
        admin_token=os.getenv("ADMIN_TOKEN", "QQliutao011007"),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "60")),
        health_check_timeout=float(os.getenv("HEALTH_CHECK_TIMEOUT", "10")),
        revival_check_interval=float(os.getenv("REVIVAL_CHECK_INTERVAL", "30")),
        url_sync_interval=float(os.getenv("URL_SYNC_INTERVAL", "3600")),
        url_sync_file=os.getenv("URL_SYNC_FILE", "").strip(),
        url_sync_group_id=int(os.getenv("URL_SYNC_GROUP_ID", "0")),
        max_calls_before_check=int(os.getenv("MAX_CALLS_BEFORE_CHECK", "3")),
        default_model=os.getenv("DEFAULT_MODEL", "gpt-4o-mini"),
    )
