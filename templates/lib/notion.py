#!/usr/bin/env python3
"""Shared Notion API client for Agent Bootstrap automations."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_BASE = "https://api.notion.com"
NOTION_VERSION = "2025-09-03"
USER_AGENT = "AgentBootstrap-NotionClient/1.0"

# v2025-09-03: 다중 data_source DB 지원. db_id → first data_source_id 캐시.
_DATA_SOURCE_CACHE: dict[str, str] = {}
BACKOFF_SECONDS = (1, 2, 4, 8, 16, 32)

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b("
    r"ntn_[A-Za-z0-9_-]{20,}|"
    r"secret_[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"xapp-[A-Za-z0-9-]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"
    r")\b"
)
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|password|passwd|authorization|api[_-]?key|access[_-]?key|refresh[_-]?token)"
)


def _log(message: str) -> None:
    print(f"[notion] {message}", file=sys.stderr, flush=True)


def _json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _token_from_mapping(data: dict[str, Any]) -> str | None:
    candidates = (
        data.get("NOTION_API_TOKEN"),
        data.get("notion_api_token"),
        data.get("notionToken"),
        data.get("notion_token"),
        data.get("env", {}).get("NOTION_API_TOKEN") if isinstance(data.get("env"), dict) else None,
        data.get("notion", {}).get("api_token") if isinstance(data.get("notion"), dict) else None,
        data.get("notion", {}).get("token") if isinstance(data.get("notion"), dict) else None,
    )
    for token in candidates:
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def get_notion_token() -> str:
    """Load NOTION_API_TOKEN from env or ~/.claude/settings.json."""

    env_token = os.environ.get("NOTION_API_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    settings_token = _token_from_mapping(_json_file(Path.home() / ".claude" / "settings.json"))
    if settings_token:
        return settings_token

    raise RuntimeError("NOTION_API_TOKEN not found in env or ~/.claude/settings.json")


def mask_secrets(value: Any) -> Any:
    """Return a copy with secret-looking keys and values redacted before sending/logging."""

    if isinstance(value, dict):
        masked: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                masked[key] = "[REDACTED]"
            else:
                masked[key] = mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_secrets(item) for item in value)
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[REDACTED]", value)
    return value


def sanitize_for_waf(value: Any) -> Any:
    """Compatibility alias: strip secret-like payload fragments that trigger WAF blocks."""

    return mask_secrets(value)


def _normalize_path(path: str) -> tuple[str, str]:
    if path.startswith("http://") or path.startswith("https://"):
        if path.startswith(API_BASE):
            display_path = path[len(API_BASE) :]
        else:
            display_path = path
        return path, display_path
    if path.startswith("/v1/"):
        return f"{API_BASE}{path}", path
    if path.startswith("v1/"):
        return f"{API_BASE}/{path}", f"/{path}"
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{API_BASE}/v1{clean_path}", f"/v1{clean_path}"


def _decode_response(response: urllib.response.addinfourl) -> dict[str, Any]:
    raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _error_payload(
    method: str,
    display_path: str,
    status: int | None = None,
    body: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "_error": True,
        "method": method,
        "path": display_path,
    }
    if status is not None:
        payload["status"] = status
    if body:
        payload["body"] = body[:1000]
    if reason:
        payload["reason"] = reason
    return payload


def _retry_wait(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return float(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])


def notion_request(method: str, path: str, body: Any = None, max_retries: int = 5) -> dict[str, Any]:
    """Call the Notion API with shared retry, token loading, and payload sanitizing.

    max_retries is the number of retry attempts after the first request.
    """

    method = method.upper()
    url, display_path = _normalize_path(path)
    safe_body = sanitize_for_waf(copy.deepcopy(body)) if body is not None else None
    data = json.dumps(safe_body, ensure_ascii=False).encode("utf-8") if safe_body is not None else None
    headers = {
        "Authorization": f"Bearer {get_notion_token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
    }

    total_attempts = max(1, max_retries + 1)
    for attempt in range(total_attempts):
        attempt_no = attempt + 1
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                result = _decode_response(response)
                _log(f"{method} {display_path} ok status={response.status} attempts={attempt_no}")
                return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if retryable and attempt < total_attempts - 1:
                wait = _retry_wait(attempt, exc.headers.get("Retry-After") if exc.headers else None)
                _log(
                    f"{method} {display_path} retry {attempt_no}/{max_retries} "
                    f"after {wait:.1f}s status={exc.code}"
                )
                time.sleep(wait)
                continue
            if retryable:
                _log(f"{method} {display_path} failed after {attempt_no} attempts status={exc.code}")
            else:
                _log(f"{method} {display_path} failed without retry status={exc.code}")
            return _error_payload(method, display_path, status=exc.code, body=detail)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt < total_attempts - 1:
                wait = _retry_wait(attempt)
                _log(
                    f"{method} {display_path} retry {attempt_no}/{max_retries} "
                    f"after {wait:.1f}s reason={exc}"
                )
                time.sleep(wait)
                continue
            _log(f"{method} {display_path} failed after {attempt_no} attempts reason={exc}")
            return _error_payload(method, display_path, reason=str(exc))

    return _error_payload(method, display_path, reason="max retries exhausted")


def rich_text_property(value: str) -> dict[str, Any]:
    """Build a Notion rich_text property value."""

    return {"rich_text": [{"type": "text", "text": {"content": str(value)[:2000]}}]}


def ensure_external_id_property(db_id: str) -> dict[str, Any]:
    """Ensure a database has the external_id rich_text property.

    Returns a small status dict:
    - {"ok": True, "status": "exists" | "created"}
    - {"ok": False, "status": "error", "error": ...}
    """

    schema = notion_request("GET", f"databases/{db_id}")
    if schema.get("_error"):
        return {"ok": False, "status": "error", "error": schema}
    properties = schema.get("properties") or {}
    existing = properties.get("external_id")
    if isinstance(existing, dict) and existing.get("type") == "rich_text":
        return {"ok": True, "status": "exists"}
    result = notion_request(
        "PATCH",
        f"databases/{db_id}",
        {"properties": {"external_id": {"rich_text": {}}}},
    )
    if result.get("_error"):
        return {"ok": False, "status": "error", "error": result}
    return {"ok": True, "status": "created"}


def _resolve_data_source_id(db_id: str) -> str:
    """v2025-09-03: 첫 data_source_id 자동 fetch + 캐시. 단일 source DB는 db_id 자체 사용."""
    if db_id in _DATA_SOURCE_CACHE:
        return _DATA_SOURCE_CACHE[db_id]
    schema = notion_request("GET", f"databases/{db_id}")
    if schema.get("_error"):
        return db_id  # 폴백
    sources = schema.get("data_sources") or []
    if not sources:
        return db_id
    ds_id = sources[0].get("id") or db_id
    _DATA_SOURCE_CACHE[db_id] = ds_id
    return ds_id


def query_database(db_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """v2025-09-03 다중 data_source 지원 query. 폴백 포함."""
    payload = body or {}
    ds_id = _resolve_data_source_id(db_id)
    if ds_id != db_id:
        result = notion_request("POST", f"data_sources/{ds_id}/query", payload)
        if not result.get("_error"):
            return result
    # 폴백: 단일 source DB 또는 신 엔드포인트 거부
    return notion_request("POST", f"databases/{db_id}/query", payload)


def query_by_external_id(db_id: str, external_id: str) -> dict[str, Any] | None:
    """Return the first page matching external_id, or None."""

    result = query_database(db_id, {
        "filter": {"property": "external_id", "rich_text": {"equals": str(external_id)}},
        "page_size": 1,
    })
    if result.get("_error"):
        raise RuntimeError(f"Notion external_id query failed: {mask_secrets(result)}")
    rows = result.get("results") or []
    return rows[0] if rows else None


def upsert_by_external_id(
    db_id: str,
    external_id: str,
    properties: dict[str, Any],
    children: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update or create a Notion page using external_id as the idempotency key.

    Existing pages only receive property updates. Children are attached only on
    first insert so reruns cannot duplicate page body blocks.
    """

    if not external_id or not str(external_id).strip():
        raise ValueError("external_id is required for Notion upsert")

    eid = str(external_id).strip()[:2000]
    merged_properties = dict(properties)
    merged_properties["external_id"] = rich_text_property(eid)

    existing = query_by_external_id(db_id, eid)
    if existing:
        result = notion_request(
            "PATCH",
            f"pages/{existing['id']}",
            {"properties": merged_properties},
        )
        if result.get("_error"):
            raise RuntimeError(f"Notion page update failed: {mask_secrets(result)}")
        result["_upsert"] = "updated"
        return result

    # v2025-09-03: 다중 data_source DB는 parent.type = data_source_id 필요
    ds_id = _resolve_data_source_id(db_id)
    if ds_id != db_id:
        parent = {"type": "data_source_id", "data_source_id": ds_id}
    else:
        parent = {"type": "database_id", "database_id": db_id}
    body: dict[str, Any] = {
        "parent": parent,
        "properties": merged_properties,
    }
    if children:
        body["children"] = children
    result = notion_request("POST", "pages", body)
    # 폴백: 신 형식 거부 시 구 형식 재시도
    if result.get("_error") and ds_id != db_id:
        body["parent"] = {"type": "database_id", "database_id": db_id}
        result = notion_request("POST", "pages", body)
    if result.get("_error"):
        raise RuntimeError(f"Notion page create failed: {mask_secrets(result)}")
    result["_upsert"] = "created"
    return result
