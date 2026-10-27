from __future__ import annotations

import hashlib
import hmac
import json
import platform
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib import request
from urllib.error import HTTPError, URLError

from . import __version__
from .constants import BEIJING_TZ


DEFAULT_TELEMETRY_ENDPOINT = "https://autotd-telemetry.autotd-buaa.workers.dev"
DEFAULT_TIMEOUT_SECONDS = 2.0


class TelemetryTransport(Protocol):
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        raise RuntimeError("本地修复版禁止遥测网络请求")


class UrlJsonTransport:
    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        raise RuntimeError("本地修复版禁止遥测网络请求")


def ensure_telemetry_state(storage, endpoint: Optional[str] = None) -> dict[str, Any]:
    return {"enabled": False, "endpoint": "", "queue_length": 0, "reason": "本地修复版永久关闭遥测"}


def enable_telemetry(storage, endpoint: Optional[str] = None) -> dict[str, Any]:
    raise ValueError("本地修复版已关闭遥测，不允许重新启用")


def disable_telemetry(storage) -> None:
    return None


def get_telemetry_status(storage) -> dict[str, Any]:
    return {"enabled": False, "endpoint": "", "queue_length": 0, "reason": "本地修复版永久关闭遥测"}


def queue_length(storage) -> int:
    return 0


def enqueue_install_initialized(storage, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    return None


def enqueue_run_requested(storage, once: bool, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    return None


def enqueue_daemon_started(storage, pid: int, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    return None


def enqueue_daemon_stopped(storage, pid: int, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    return None


def enqueue_stop_requested(storage, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    return None


def enqueue_user_changed(
    storage,
    change_type: str,
    affected_student_id: str,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    return None


def enqueue_snapshot_event(
    storage,
    event_type: str,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    return None


def enqueue_td_count_changed(
    storage,
    student_id: str,
    previous_count: Optional[int],
    new_count: int,
    now: Optional[datetime] = None,
    count_source: str = "observation",
) -> Optional[dict[str, Any]]:
    return None


def enqueue_daily_midnight_snapshot(storage, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    return None


def enqueue_event(
    storage,
    event_type: str,
    payload: dict[str, Any],
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    return None


def flush_telemetry_queue(
    storage,
    transport: Optional[TelemetryTransport] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    return 0


def _registration_payload(storage, telemetry: dict[str, Any]) -> dict[str, Any]:
    return {
        "installation_id": telemetry["installation_id"],
        "installation_secret": telemetry["installation_secret"],
        "app_version": __version__,
        "platform": platform.platform(),
        "registered_at": _local_now(None).isoformat(timespec="seconds"),
        **_snapshot_payload(storage),
    }


def _register_installation(
    storage,
    transport: TelemetryTransport,
    endpoint: str,
    telemetry: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    transport.post_json(
        _url(endpoint, "/v1/installations/register"),
        _registration_payload(storage, telemetry),
        timeout=timeout,
    )
    _mark_registered(storage)
    return ensure_telemetry_state(storage)


def _post_event(
    storage,
    transport: TelemetryTransport,
    endpoint: str,
    telemetry: dict[str, Any],
    body: dict[str, Any],
    timeout: float,
) -> None:
    del storage
    signature = _signature(telemetry["installation_secret"], body)
    transport.post_json(
        _url(endpoint, "/v1/events"),
        body,
        headers={"X-AutoTD-Installation": telemetry["installation_id"], "X-AutoTD-Signature": signature},
        timeout=timeout,
    )


def _snapshot_payload(storage) -> dict[str, Any]:
    users = []
    counts = storage.load_state().get("counts", {})
    for user in sorted(storage.list_users(), key=lambda item: item.student_id):
        count_info = counts.get(user.student_id) or {}
        users.append(
            {
                "student_id": user.student_id,
                "td_count": count_info.get("count"),
            }
        )
    return {
        "current_user_count": len(users),
        "users": users,
    }


def _queue_path(storage) -> Path:
    return getattr(storage, "telemetry_queue_path", storage.home / "telemetry_queue.jsonl")


def _append_queue(storage, event: dict[str, Any]) -> None:
    path = _queue_path(storage)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        f.write("\n")


def _read_queue(storage) -> list[dict[str, Any]]:
    path = _queue_path(storage)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _write_queue(storage, events: list[dict[str, Any]]) -> None:
    path = _queue_path(storage)
    if not events:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")


def _mark_registered(storage) -> None:
    state = storage.load_state()
    state.setdefault("telemetry", {})["registered"] = True
    storage.save_state(state)


def _mark_unregistered(storage) -> None:
    state = storage.load_state()
    state.setdefault("telemetry", {})["registered"] = False
    storage.save_state(state)


def _is_unknown_installation_error(exc: Exception) -> bool:
    return "unknown_installation" in str(exc)


def _record_sent(storage) -> None:
    state = storage.load_state()
    telemetry = state.setdefault("telemetry", {})
    telemetry["last_sent_at"] = _local_now(None).isoformat(timespec="seconds")
    telemetry["last_error"] = None
    storage.save_state(state)


def _record_last_error(storage, message: str) -> None:
    state = storage.load_state()
    telemetry = state.setdefault("telemetry", {})
    telemetry["last_error"] = message
    storage.save_state(state)


def _url(endpoint: str, path: str) -> str:
    return endpoint.rstrip("/") + path


def _signature(secret: str, payload: dict[str, Any]) -> str:
    return hmac.new(secret.encode("utf-8"), _json_body(payload), hashlib.sha256).hexdigest()


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _local_now(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(BEIJING_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)
