from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_base_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    value = value.rstrip("/")
    if not (value.startswith("http://") or value.startswith("https://")):
        return ""

    parts = urlsplit(value)
    path = parts.path or ""
    if path in {"", "/"}:
        path = "/api"
    path = path.rstrip("/")
    if not path:
        path = "/api"

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
