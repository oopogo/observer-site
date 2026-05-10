#!/usr/bin/env python3
"""Observer site backend.

Serves the static homepage, live /api/agents monitoring, and a guarded
agent-chat bridge for the three allowed OpenClaw agents.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".data"
HISTORY_PATH = DATA_DIR / "chat-history.json"
SETTINGS_PATH = DATA_DIR / "agent-settings.json"
READ_STATE_PATH = DATA_DIR / "chat-read-state.json"
UPLOAD_DIR = DATA_DIR / "uploads"
CLEANUP_STATE_PATH = DATA_DIR / "session-cleanup-state.json"
SESSION_ARCHIVE_DIR = DATA_DIR / "session-archives"
ACTIVE_CHAT_SESSIONS_PATH = DATA_DIR / "active-chat-sessions.json"
REPORT_TS_CACHE_PATH = DATA_DIR / "report-ts-cache.json"
WORK_STATE_PATH = DATA_DIR / "work-state.json"
SELF_WORK_STATE_PATH = DATA_DIR / "observer-self-work.json"
MYLENE_HEARTBEAT_STATE_PATH = Path("/home/oopogo/.openclaw/workspace/memory/heartbeat-state.json")
ACP_SESSION_DIRS = [
    Path("/home/oopogo/.openclaw/workspace/state/sessions"),
    Path("/home/oopogo/.openclaw/workspace-observer/state/sessions"),
]
ROGUELIKE_ROOT = Path("/mnt/c/MegaGrit/RogueLike_001")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "/home/oopogo/.npm-global/bin/openclaw")
AGENTS = [
    {"id": "main", "name": "밀레느", "orb": "밀", "role": "메인 작업 에이전트", "tags": ["MAIN", "ORCHESTRATOR", "게임"]},
    {"id": "observer", "name": "observer", "orb": "옵", "role": "운영 감시자", "tags": ["OPS", "GATEWAY", "감시"]},
    {"id": "mediacontentproducer", "name": "미디어", "orb": "미", "role": "콘텐츠 프로듀서", "tags": ["MEDIA", "CRON-PAUSED", "콘텐츠"]},
]
CACHE_TTL_SECONDS = 8
MAX_SETTINGS_BODY_BYTES = 16_000_000
MAX_CHAT_IMAGE_BYTES = 8_000_000
MAX_CHAT_IMAGES = 6
AGENTS_CACHE: dict[str, Any] | None = None
GATEWAY_CACHE: dict[str, Any] | None = None
SESSIONS_CACHE: dict[str, Any] | None = None
HISTORY_SYNC_TS: dict[str, float] = {}
SANITIZE_SYNC_TS = 0.0
CACHE_LOCK = threading.Lock()

NUDGE_PATH = DATA_DIR / "recovery-nudges.json"
NUDGE_INTERVAL_SECONDS = 900
SILENCE_NUDGE_INTERVAL_SECONDS = 900
# 세션 목록의 running 상태는 비정상 종료/중단 뒤에도 남을 수 있다.
# 관제 화면은 "현재 작업"만 보여야 하므로, 최근 갱신이 없는 running 세션은
# 작업/지연으로 올리지 않고 고아 세션으로 제외한다.
ACTIVE_SESSION_MAX_STALE_MS = 3 * 60 * 1000
SUBAGENT_ACTIVE_MAX_STALE_MS = 3 * 60 * 1000
ASSIGNED_MAX_AGE_MS = 30 * 60 * 1000
DEFAULT_CONTEXT_MAX_TOKENS = 1_000_000
ORPHAN_RUNNING_CANDIDATE_MS = 6 * 60 * 60 * 1000
ARCHIVE_CANDIDATE_MS = 3 * 60 * 60 * 1000
CONTEXT_ROLLOVER_RATIO = 0.95
SILENCE_WARNING_MS = 2 * 60 * 1000
SELF_WORK_WARNING_MS = 2 * 60 * 1000
SELF_WORK_REPORT_INTERVAL_MS = 5 * 60 * 1000
SILENCE_AUTO_NUDGE_ENABLED = True


def cache_get(name: str) -> dict[str, Any] | None:
    with CACHE_LOCK:
        cached = AGENTS_CACHE if name == "agents" else (GATEWAY_CACHE if name == "gateway" else SESSIONS_CACHE)
        if not isinstance(cached, dict):
            return None
        age_ms = int(time.time() * 1000) - int(cached.get("generatedAt") or 0)
        return {**cached, "cacheAgeMs": max(0, age_ms)}


def cache_set(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    global AGENTS_CACHE, GATEWAY_CACHE, SESSIONS_CACHE
    with CACHE_LOCK:
        if name == "agents":
            AGENTS_CACHE = payload
        elif name == "gateway":
            GATEWAY_CACHE = payload
        else:
            SESSIONS_CACHE = payload
    return payload


def invalidate_agents_cache() -> None:
    global AGENTS_CACHE
    with CACHE_LOCK:
        AGENTS_CACHE = None


def get_sessions_cached(timeout: int = 12) -> list[dict[str, Any]]:
    cached = cache_get("sessions")
    if cache_is_fresh(cached):
        sessions = cached.get("sessions") if isinstance(cached, dict) else []
        return sessions if isinstance(sessions, list) else []
    payload = gateway_call("sessions.list", {"limit": 120}, timeout=timeout)
    sessions = payload.get("sessions") if isinstance(payload, dict) else []
    if not isinstance(sessions, list):
        sessions = []
    cache_set("sessions", {"generatedAt": int(time.time() * 1000), "sessions": sessions})
    return sessions


def cache_is_fresh(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    age_ms = int(time.time() * 1000) - int(payload.get("generatedAt") or 0)
    return age_ms <= CACHE_TTL_SECONDS * 1000


def stale_payload(name: str, error: Exception) -> dict[str, Any] | None:
    cached = cache_get(name)
    if not cached:
        return None
    cached["stale"] = True
    cached["warning"] = f"최근 정상 상태를 표시 중입니다: {error}"
    return cached


def load_agent_settings() -> dict[str, Any]:
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_agent_settings(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def apply_agent_settings(agent: dict[str, Any]) -> dict[str, Any]:
    settings = load_agent_settings().get(str(agent.get("id")), {})
    if not isinstance(settings, dict):
        settings = {}
    merged = dict(agent)
    display_name = settings.get("displayName")
    if isinstance(display_name, str) and display_name.strip():
        merged["name"] = display_name.strip()[:40]
    avatar = settings.get("avatar")
    if isinstance(avatar, str) and avatar.startswith("data:image/") and len(avatar) <= 2_000_000:
        merged["avatar"] = avatar
    return merged


def save_single_agent_setting(agent_id: str, display_name: str | None, avatar: str | None) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    data = load_agent_settings()
    current = data.get(agent_id, {}) if isinstance(data.get(agent_id), dict) else {}
    if display_name is not None:
        current["displayName"] = display_name.strip()[:40]
    if avatar is not None:
        if avatar and (not avatar.startswith("data:image/") or len(avatar) > 2_000_000):
            raise ValueError("이미지는 2MB 이하의 이미지 파일만 사용할 수 있습니다.")
        if avatar:
            current["avatar"] = avatar
        else:
            current.pop("avatar", None)
    data[agent_id] = current
    save_agent_settings(data)
    return {"ok": True, "agentId": agent_id, "settings": current}



def load_read_state() -> dict[str, int]:
    try:
        data = json.loads(READ_STATE_PATH.read_text())
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_read_state(data: dict[str, int]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    READ_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def mark_agent_read(agent_id: str) -> None:
    state = load_read_state()
    now_ms = int(time.time() * 1000)
    previous = int(state.get(agent_id, 0) or 0)
    # Chat panes poll frequently. Avoid rewriting read-state and invalidating
    # /api/agents cache on every poll; once every 10s is enough for badges.
    if now_ms - previous < 10_000:
        return
    state[agent_id] = now_ms
    save_read_state(state)
    invalidate_agents_cache()


def unread_count(agent_id: str) -> int:
    read_state = load_read_state()
    if agent_id not in read_state:
        return 0
    last_read = read_state.get(agent_id, 0)
    count = 0
    for item in load_history().get(agent_id, []):
        ts = int(item.get("ts") or 0)
        if ts <= last_read:
            continue
        role = item.get("role")
        status = item.get("status")
        content = str(item.get("content") or "")
        if item.get("hidden"):
            continue
        if role == "user" or status in {"pending", "expired", "stale-pending", "recovery-requested", "abort-requested", "filtered-rawdump", "filtered-reasoning", "internal-status"}:
            continue
        if content.startswith("전달됨") or content.startswith("응답 대기") or content.startswith("관제 화면에서 상태 재확인"):
            continue
        count += 1
    return count

def parse_json_suffix(raw: str) -> Any:
    cleaned = raw.strip()
    starts = [i for i, ch in enumerate(cleaned) if ch in "{["]
    for start in reversed(starts):
        try:
            return json.loads(cleaned[start:])
        except Exception:
            continue
    raise ValueError(f"No JSON payload in output: {cleaned[:300]}")


def read_gateway_token() -> str:
    try:
        cfg = json.loads(Path("/home/oopogo/.openclaw/openclaw.json").read_text())
        token = cfg.get("gateway", {}).get("auth", {}).get("token")
        return token if isinstance(token, str) else ""
    except Exception:
        return ""


def read_body_json(handler: SimpleHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0 or length > MAX_SETTINGS_BODY_BYTES:
        raise ValueError("invalid request body size")
    raw = handler.rfile.read(length).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("request body must be an object")
    return data


def load_history() -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(HISTORY_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_history(history: dict[str, list[dict[str, Any]]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2))


def load_work_state() -> dict[str, Any]:
    try:
        data = json.loads(WORK_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_work_state(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    WORK_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    invalidate_agents_cache()


def load_self_work_state() -> dict[str, Any]:
    try:
        data = json.loads(SELF_WORK_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_self_work_state(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    SELF_WORK_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    invalidate_agents_cache()


def register_self_work(message: str, request_id: str | None = None) -> dict[str, Any]:
    now = int(time.time() * 1000)
    data = load_self_work_state()
    item = {
        "requestId": request_id or f"self-{uuid.uuid4()}",
        "status": "working",
        "message": str(message or "").strip()[:500],
        "startedAt": now,
        "updatedAt": now,
        "lastReportAt": 0,
    }
    items = data.setdefault("items", [])
    if not isinstance(items, list):
        items = []
        data["items"] = items
    items.append(item)
    del items[:-50]
    save_self_work_state(data)
    return item


def update_self_work(request_id: str, status: str, report: str | None = None, *, append_chat_report: bool = False) -> dict[str, Any] | None:
    now = int(time.time() * 1000)
    data = load_self_work_state()
    items = data.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("requestId") != request_id:
            continue
        item["status"] = status
        item["updatedAt"] = now
        if report:
            item["report"] = report[:1000]
            item["lastReportAt"] = now
            item["reportedAt"] = now
        save_self_work_state(data)
        if append_chat_report and report:
            append_history("observer", "assistant", str(report), {
                "requestId": request_id,
                "sessionKey": observer_chat_session_key("observer"),
                "status": "done",
                "done": True,
                "pending": False,
                "selfWorkReport": True,
            })
        return item
    return None


def active_self_work_item() -> dict[str, Any] | None:
    items = load_self_work_state().get("items", [])
    if not isinstance(items, list):
        return None
    active = [item for item in items if isinstance(item, dict) and item.get("status") in {"working", "reporting", "verifying"}]
    if not active:
        return None
    return max(active, key=lambda item: int(item.get("updatedAt") or item.get("startedAt") or 0))


def register_work_item(agent_id: str, request_id: str, message: str, session_key: str) -> None:
    now = int(time.time() * 1000)
    data = load_work_state()
    items = data.setdefault(agent_id, [])
    if not isinstance(items, list):
        items = []
        data[agent_id] = items
    items.append({
        "requestId": request_id,
        "sessionKey": session_key,
        "status": "working",
        "startedAt": now,
        "updatedAt": now,
        "lastReportAt": 0,
        "lastNudgeAt": 0,
        "message": str(message or "").strip()[:500],
    })
    del items[:-30]
    save_work_state(data)


def update_work_item(agent_id: str, request_id: str, status: str, *, report: bool = False, reason: str | None = None) -> None:
    now = int(time.time() * 1000)
    data = load_work_state()
    items = data.get(agent_id)
    if not isinstance(items, list):
        return
    changed = False
    for item in items:
        if not isinstance(item, dict) or item.get("requestId") != request_id:
            continue
        item["status"] = status
        item["updatedAt"] = now
        if report:
            item["lastReportAt"] = now
            item["reportedAt"] = now
        if reason:
            item["reason"] = reason[:500]
        changed = True
        break
    if changed:
        save_work_state(data)


def close_latest_active_work_from_report(agent_id: str, session_key: str, text: str, ts: int | None = None) -> bool:
    if not text or is_internal_status_text(text) or is_progress_only_report_text(text):
        return False
    now = int(time.time() * 1000)
    report_ts = ts or now
    failed = text.startswith("실패:") or "실패했습니다" in text or "중단했습니다" in text or "못했습니다" in text
    data = load_work_state()
    items = data.get(agent_id)
    if not isinstance(items, list):
        return False
    candidates = [
        item for item in items
        if isinstance(item, dict)
        and item.get("status") in {"working", "reporting", "verifying"}
        and int(item.get("startedAt") or 0) <= report_ts
        and (not session_key or item.get("sessionKey") == session_key)
    ]
    if not candidates:
        return False
    item = max(candidates, key=lambda x: int(x.get("updatedAt") or x.get("startedAt") or 0))
    item["status"] = "failed" if failed else "reported"
    item["updatedAt"] = now
    item["lastReportAt"] = report_ts
    item["reportedAt"] = report_ts
    if failed:
        item["reason"] = text[:500]
    save_work_state(data)
    return True

def active_work_item(agent_id: str) -> dict[str, Any] | None:
    items = load_work_state().get(agent_id, [])
    if not isinstance(items, list):
        return None
    active = [item for item in items if isinstance(item, dict) and item.get("status") in {"working", "reporting", "verifying"}]
    if not active:
        return None
    active.sort(key=lambda item: int(item.get("updatedAt") or item.get("startedAt") or 0), reverse=True)
    return active[0]


def mark_work_nudged(agent_id: str, request_id: str) -> None:
    now = int(time.time() * 1000)
    data = load_work_state()
    items = data.get(agent_id)
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict) and item.get("requestId") == request_id:
            item["lastNudgeAt"] = now
            item["updatedAt"] = now
            break
    save_work_state(data)


def load_cleanup_state() -> dict[str, Any]:
    try:
        data = json.loads(CLEANUP_STATE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cleanup_state(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    CLEANUP_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_active_chat_sessions() -> dict[str, str]:
    try:
        data = json.loads(ACTIVE_CHAT_SESSIONS_PATH.read_text())
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_active_chat_sessions(data: dict[str, str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    ACTIVE_CHAT_SESSIONS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def set_active_chat_session(agent_id: str, key: str) -> None:
    data = load_active_chat_sessions()
    data[agent_id] = key
    save_active_chat_sessions(data)
    invalidate_agents_cache()


def cleanup_state_set(agent_id: str, key: str, kind: str) -> None:
    data = load_cleanup_state()
    bucket = data.setdefault(agent_id, {})
    bucket[key] = {"kind": kind, "ts": int(time.time() * 1000)}
    save_cleanup_state(data)
    invalidate_agents_cache()


def cleanup_state_kind(agent_id: str, key: str) -> str | None:
    data = load_cleanup_state().get(agent_id, {})
    item = data.get(key) if isinstance(data, dict) else None
    return str(item.get("kind")) if isinstance(item, dict) else None


def agent_session_store_path(agent_id: str) -> Path:
    safe = ''.join(ch for ch in agent_id if ch.isalnum() or ch in {'-', '_'})
    return Path("/home/oopogo/.openclaw/agents") / safe / "sessions" / "sessions.json"


def append_history(agent_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    history = load_history()
    item = {"role": role, "content": content, "ts": int(time.time() * 1000), **(meta or {})}
    entries = history.setdefault(agent_id, [])
    entries.append(item)
    del entries[:-80]
    save_history(history)
    invalidate_agents_cache()
    return item


def replace_history_message(agent_id: str, request_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    content = strip_internal_report_contract(content)
    history = load_history()
    entries = history.setdefault(agent_id, [])
    for index, item in enumerate(entries):
        if item.get("requestId") == request_id and item.get("role") == role:
            updated = {**item, "content": content, "ts": int(time.time() * 1000), **(meta or {})}
            entries[index] = updated
            save_history(history)
            invalidate_agents_cache()
            return updated
    return None


def extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text") or item.get("value")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def extract_session_message_text(message: dict[str, Any]) -> str:
    return extract_content_text(message.get("content"))



def base_observer_chat_session_key(agent_id: str) -> str:
    return f"agent:{agent_id}:observer-site-chat"


def desired_observer_chat_session_key(agent_id: str) -> str:
    configured = load_active_chat_sessions().get(agent_id)
    prefix = base_observer_chat_session_key(agent_id)
    if configured and (configured == prefix or configured.startswith(prefix + ":")):
        return configured
    return prefix


def agent_session_key_rows(agent_id: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        key = str(session.get("key") or "")
        if not key.startswith(f"agent:{agent_id}:"):
            continue
        if ":subagent:" in key or ":cron:" in key or ":openresponses:" in key:
            continue
        rows.append(session)
    rows.sort(key=lambda item: to_epoch_ms(item.get("updatedAt") or item.get("startedAt")), reverse=True)
    return rows


def existing_agent_session_keys(agent_id: str) -> list[dict[str, Any]]:
    try:
        payload = gateway_call("sessions.list", {"limit": 120}, timeout=12)
    except Exception:
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else []
    return agent_session_key_rows(agent_id, sessions if isinstance(sessions, list) else [])


def observer_chat_session_key_from_rows(agent_id: str, rows: list[dict[str, Any]]) -> str:
    desired = desired_observer_chat_session_key(agent_id)
    base_key = base_observer_chat_session_key(agent_id)
    keys = [str(row.get("key") or "") for row in rows]
    if desired in keys:
        return desired
    rotated = [key for key in keys if key.startswith(base_key + ":")]
    if rotated:
        rotated.sort(reverse=True)
        return rotated[0]
    if base_key in keys:
        return base_key
    return desired


def observer_chat_session_key(agent_id: str) -> str:
    """Return an existing sendable session key for the agent."""
    return observer_chat_session_key_from_rows(agent_id, existing_agent_session_keys(agent_id))


def session_context_usage_ratio(row: dict[str, Any] | None) -> float:
    if not isinstance(row, dict):
        return 0.0
    total = float(row.get("totalTokens") or 0)
    ctx = float(row.get("contextTokens") or DEFAULT_CONTEXT_MAX_TOKENS or 0)
    return total / ctx if ctx > 0 else 0.0


def current_observer_chat_row_from_sessions(agent_id: str, sessions: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    rows = agent_session_key_rows(agent_id, sessions)
    key = observer_chat_session_key_from_rows(agent_id, rows)
    for row in rows:
        if str(row.get("key") or "") == key:
            return key, row
    return key, None


def current_observer_chat_row(agent_id: str) -> dict[str, Any] | None:
    key = observer_chat_session_key(agent_id)
    for row in existing_agent_session_keys(agent_id):
        if str(row.get("key") or "") == key:
            return row
    return None


def compact_local_chat_summary(agent_id: str, limit: int = 12) -> str:
    rows = public_history(agent_id, mark_read=False).get("messages", [])[-limit:]
    lines = []
    for item in rows:
        role = item.get("role") or "message"
        text = strip_internal_report_contract(str(item.get("content") or "")).replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"- {role}: {text[:220]}")
    return "\n".join(lines[-limit:])


def recent_completion_report_for_handoff(agent_id: str, limit: int = 80) -> str:
    rows = public_history(agent_id, mark_read=False).get("messages", [])[-limit:]
    for item in reversed(rows):
        if item.get("role") != "assistant":
            continue
        text = strip_internal_report_contract(str(item.get("content") or "")).strip()
        if not text or is_internal_status_text(text) or is_progress_only_report_text(text):
            continue
        lowered = text.lower()
        completion_markers = ("완료보고", "완료 보고", "완료했습니다", "완료했습니다.", "커밋/push", "커밋/푸시", "검증:", "검증 결과")
        if any(marker in text for marker in completion_markers) or "commit:" in lowered or "push 완료" in text:
            return text[:1600]
    return ""


def build_observer_handoff_text(agent_id: str, summary: str) -> str:
    latest_report = recent_completion_report_for_handoff(agent_id)
    lines = [
        "[이전 관제 대화 요약]",
        summary or "요약할 최근 대화가 없습니다.",
    ]
    if latest_report:
        lines.extend([
            "",
            "[최근 완료보고 후보 - 현재 채팅에 다시 보여줘야 할 수 있음]",
            latest_report,
        ])
    lines.extend([
        "",
        "[세션 연속성 규칙]",
        "이 메시지는 새 세션이 아니라 같은 관제 대화의 이어받기입니다.",
        "이전 요약이나 최근 완료보고 후보에 완료/검증/커밋/차단 내용이 있고 현재 채팅에 보이지 않는다는 지적이 오면, 새 작업을 시작하지 말고 현재 채팅에 완료보고를 즉시 재게시하세요.",
        "'이전 세션에 있습니다'만 말하는 것은 실패입니다. 원인 설명보다 현재 채팅 기준 최종 산출물·검증·실패/차단을 먼저 보고하세요.",
        "",
        "위 요약을 참고해 새 관제 세션에서 이어서 답하세요.",
    ])
    return "\n".join(lines)


def create_new_observer_chat_session(agent_id: str, reason: str = "manual") -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    old_key = observer_chat_session_key(agent_id)
    stamp = time.strftime("%Y%m%d%H%M%S")
    new_key = f"agent:{agent_id}:observer-site-chat:{stamp}"
    result = gateway_call("sessions.create", {"agentId": agent_id, "key": new_key}, timeout=18)
    set_active_chat_session(agent_id, new_key)
    append_history(agent_id, "system", f"관제 채팅 새 세션 전환: {old_key} → {new_key}", {"status": "session-rollover", "oldSessionKey": old_key, "sessionKey": new_key, "reason": reason})
    return {"ok": True, "agentId": agent_id, "oldSessionKey": old_key, "sessionKey": new_key, "result": result}


def maybe_rollover_chat_session_from_sessions(agent_id: str, sessions: list[dict[str, Any]]) -> tuple[str, str | None]:
    key, row = current_observer_chat_row_from_sessions(agent_id, sessions)
    if session_context_usage_ratio(row) < CONTEXT_ROLLOVER_RATIO:
        return key, None
    summary = compact_local_chat_summary(agent_id)
    result = create_new_observer_chat_session(agent_id, "context-auto")
    handoff = build_observer_handoff_text(agent_id, summary)
    return result["sessionKey"], handoff


def maybe_rollover_chat_session(agent_id: str) -> tuple[str, str | None]:
    try:
        payload = gateway_call("sessions.list", {"limit": 120}, timeout=12)
        sessions = payload.get("sessions") if isinstance(payload, dict) else []
    except Exception:
        sessions = []
    return maybe_rollover_chat_session_from_sessions(agent_id, sessions if isinstance(sessions, list) else [])


def sync_gateway_session_history(agent_id: str) -> None:
    session_key = observer_chat_session_key(agent_id)
    try:
        payload = gateway_call("sessions.get", {"key": session_key, "limit": 40}, timeout=12)
    except Exception:
        return
    messages = payload.get("messages") if isinstance(payload, dict) else []
    if not isinstance(messages, list):
        return
    history = load_history()
    entries = history.setdefault(agent_id, [])
    existing = {(item.get("role"), item.get("content")) for item in entries}
    appended = False
    latest_synced_ts = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        text = extract_session_message_text(message)
        if not text:
            continue
        key = ("assistant", text)
        if key in existing:
            continue
        ts = to_epoch_ms(message.get("timestamp") or message.get("ts") or message.get("createdAt")) or int(time.time() * 1000)
        entries.append({"role": "assistant", "content": text, "ts": ts, "status": "synced", "sessionKey": session_key})
        existing.add(key)
        latest_synced_ts = max(latest_synced_ts, ts)
        appended = True
    if appended:
        for item in entries:
            if item.get("role") == "system" and item.get("status") in {"pending", "fallback"} and int(item.get("ts") or 0) <= latest_synced_ts:
                item["hidden"] = True
                item["status"] = "done"
                item["done"] = True
                item.pop("pending", None)
        del entries[:-80]
        save_history(history)


def sync_latest_session_file_report(agent_id: str) -> int:
    session_key = observer_chat_session_key(agent_id)
    sessions_path = agent_session_store_path(agent_id)
    try:
        sessions_data = json.loads(sessions_path.read_text())
    except Exception:
        return 0
    row = sessions_data.get(session_key) if isinstance(sessions_data, dict) else None
    if not isinstance(row, dict) or not row.get("sessionFile"):
        return 0
    path = Path(str(row.get("sessionFile")))
    if not path.is_file():
        return 0
    history = load_history()
    entries = history.setdefault(agent_id, [])
    latest_user_ts = max((int(item.get("ts") or 0) for item in entries if item.get("role") == "user"), default=0)
    existing = {(item.get("role"), item.get("content")) for item in entries}
    appended = 0
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 2_000_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()[-500:]
    except Exception:
        return 0
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        ts = to_epoch_ms(row.get("timestamp"))
        if latest_user_ts and ts and ts < latest_user_ts:
            continue
        text = extract_content_text(message.get("content"))
        if not text or is_internal_status_text(text) or text == "NO_REPLY":
            continue
        key = ("assistant", text)
        if key in existing:
            continue
        entries.append({"role": "assistant", "content": text, "ts": ts or int(time.time() * 1000), "status": "synced", "visibleSync": True, "sessionKey": session_key})
        existing.add(key)
        appended += 1
        close_latest_active_work_from_report(agent_id, session_key, text, ts or None)
    if appended:
        del entries[:-80]
        save_history(history)
        invalidate_agents_cache()
    return appended


def maybe_sync_history_light(agent_id: str) -> None:
    global SANITIZE_SYNC_TS
    now = time.time()
    # These scans are expensive on large transcript/history files. They are not
    # needed for every 5s UI poll; run them opportunistically at most every 30s.
    if now - SANITIZE_SYNC_TS >= 30:
        sanitize_internal_done_messages()
        SANITIZE_SYNC_TS = now
    last = HISTORY_SYNC_TS.get(agent_id, 0.0)
    if now - last >= 30:
        sync_latest_session_file_report(agent_id)
        HISTORY_SYNC_TS[agent_id] = now


def public_history(agent_id: str, mark_read: bool = False, sync: bool = False) -> dict[str, Any]:
    if sync:
        maybe_sync_history_light(agent_id)
    # 중요: 채팅창은 로컬 브리지 히스토리만 즉시 반환한다.
    # Gateway sessions.get 동기화는 느리고, observer처럼 작업 세션과 관제 채팅
    # 세션키가 겹칠 때 도구 로그/다른 흐름을 채팅창에 섞는다.
    if mark_read:
        mark_agent_read(agent_id)
    messages = load_history().get(agent_id, [])[-80:]

    def is_visible(item: dict[str, Any]) -> bool:
        if item.get("hidden"):
            return False
        status = item.get("status")
        if status in {"stale-pending", "expired"}:
            return False
        if item.get("role") == "assistant" and status == "synced" and not item.get("visibleSync"):
            return False
        content = str(item.get("content") or "")
        if status == "pending":
            return True
        if "응답 연결이 잠시 끊겨" in content or "응답 대기가 만료" in content:
            return False
        if content.startswith("백그라운드 실행을 시작"):
            return False
        return not is_internal_status_text(content)

    visible = []
    seen_content_by_base_request: dict[str, set[str]] = {}
    for item in messages:
        if not is_visible(item):
            continue
        content = strip_internal_report_contract(str(item.get("content") or ""))
        request_id = str(item.get("requestId") or "")
        base_request_id = request_id.removeprefix("silence-")
        normalized_content = " ".join(content.split())
        if request_id.startswith("silence-") and normalized_content in seen_content_by_base_request.get(base_request_id, set()):
            # Auto silence nudge can race with the original turn and receive the
            # exact same assistant reply. Keep the audit trail in storage, but
            # do not show duplicate bubbles in the operator chat.
            continue
        if base_request_id:
            seen_content_by_base_request.setdefault(base_request_id, set()).add(normalized_content)
        if "[관제 보고 규칙]" in str(item.get("content") or ""):
            item = {**item, "content": content}
        visible.append(item)
    return {"ok": True, "agentId": agent_id, "messages": visible[-80:]}

def extract_response_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    for key in ("output_text", "text", "message", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if key == "content":
            content_text = extract_content_text(value)
            if content_text:
                return content_text
    parts: list[str] = []
    for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict):
                text = content.get("text") or content.get("value")
                if isinstance(text, str):
                    parts.append(text)
    if parts:
        return "\n".join(parts).strip()
    return json.dumps(payload, ensure_ascii=False)[:4000]



def is_transport_status_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("runId"), str) and str(payload.get("status") or "").lower() in {"started", "running", "queued", "pending"}


def latest_assistant_text_from_session(session_key: str, after_ms: int) -> str | None:
    try:
        payload = gateway_call("sessions.get", {"key": session_key, "limit": 20}, timeout=12)
    except Exception:
        return None
    messages = payload.get("messages") if isinstance(payload, dict) else []
    if not isinstance(messages, list):
        return None
    best_ts = 0
    best_text = None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        ts = to_epoch_ms(message.get("timestamp") or message.get("ts") or message.get("createdAt"))
        if ts and ts < after_ms:
            continue
        text = extract_session_message_text(message)
        if not text:
            text = extract_response_text(message)
        if not text or is_internal_status_text(text):
            continue
        if ts >= best_ts:
            best_ts = ts
            best_text = text
    return best_text


def wait_for_agent_reply(session_key: str, after_ms: int, timeout_seconds: int = 900) -> str | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        text = latest_assistant_text_from_session(session_key, after_ms)
        if text:
            return text
        time.sleep(1.0)
    return None


def wait_for_non_progress_agent_reply(session_key: str, after_ms: int, timeout_seconds: int = 900) -> str | None:
    """Wait for the latest visible assistant reply that is not just a start/progress report."""
    deadline = time.time() + timeout_seconds
    latest_progress: str | None = None
    while time.time() < deadline:
        text = latest_assistant_text_from_session(session_key, after_ms)
        if text and not is_internal_status_text(text):
            if not is_progress_only_report_text(text):
                return text
            latest_progress = text
        time.sleep(1.0)
    return latest_progress



def is_internal_status_text(text: str) -> bool:
    stripped = text.strip()
    stripped = stripped.removeprefix("⚠️").strip()
    if not stripped:
        return True
    if stripped in {"NO_REPLY", "HEARTBEAT_OK"}:
        return True
    if stripped.startswith('{'):
        if '"role": "assistant"' in stripped and ('"type": "thinking"' in stripped or '"type": "toolCall"' in stripped or '"type": "toolResult"' in stripped):
            return True
        try:
            payload = json.loads(stripped)
            if is_transport_status_payload(payload):
                return True
            if isinstance(payload, dict) and payload.get("role") == "assistant":
                content = payload.get("content")
                # Reasoning/tool-only assistant payloads are internal artifacts,
                # not user-visible completion reports.
                if isinstance(content, list):
                    has_tool_or_reasoning = any(isinstance(item, dict) and item.get("type") in {"thinking", "toolCall", "toolResult"} for item in content)
                    text = extract_content_text(content)
                    if not text or has_tool_or_reasoning:
                        return True
        except Exception:
            pass
    bad_prefixes = (
        "백그라운드 실행을 시작",
        "메시지는 일반 세션",
        "응답 대기가 만료",
        "응답 도착",
        "전달됨.",
        "Agent failed before reply",
        "All models failed",
        "Logs: openclaw logs",
    )
    return stripped.startswith(bad_prefixes)


def strip_internal_report_contract(text: str) -> str:
    value = str(text or "")
    marker = "[관제 보고 규칙]"
    index = value.find(marker)
    if index < 0:
        return value
    return value[:index].rstrip()


def is_progress_only_report_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value or is_internal_status_text(value):
        return False
    completion_markers = (
        "완료했습니다", "처리했습니다", "끝났습니다", "마쳤습니다", "완료됐습니다",
        "작업 완료", "완료 상태", "검증", "커밋", "push", "업로드 완료",
        "실패:", "실패했습니다", "중단했습니다", "못했습니다",
    )
    if any(marker in value for marker in completion_markers):
        return False
    # Only block very explicit start-only reports. Do not treat ordinary answers
    # like "확인했습니다 ... 기준을 바꾸겠습니다" as incomplete.
    future_report_markers = ("완료되면", "완료 전까지", "끝나면", "보고하겠습니다", "보고드리겠습니다", "완료 후", "끝나는 대로")
    start_markers = ("시작합니다", "시작했습니다", "진행하겠습니다", "진행합니다", "착수합니다", "바로 넣겠습니다", "바로 수정", "확인하겠습니다", "보겠습니다")
    if any(marker in value for marker in future_report_markers):
        return True
    return any(marker in value for marker in start_markers) and len(value) < 500


def validate_chat_message(agent_id: str, message: str, attachments: list[dict[str, Any]] | None = None) -> None:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    if not message.strip() and not attachments:
        raise ValueError("메시지나 이미지가 필요합니다.")
    if len(message) > 8000:
        raise ValueError("메시지가 너무 깁니다. 8000자 이하로 보내주세요.")
    lowered = message.lower()
    blocked = [
        "systemctl --user restart openclaw-gateway",
        "systemctl --user stop openclaw-gateway",
        "openclaw gateway restart",
        "openclaw gateway stop",
        "config.patch",
        "config.set",
    ]
    if any(term in lowered for term in blocked):
        raise ValueError("이 채팅창에서는 게이트웨이 재시작/중지/라이브 설정 변경 지시를 막아두었습니다.")


def save_chat_attachments(agent_id: str, request_id: str, attachments: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not attachments:
        return []
    if len(attachments) > MAX_CHAT_IMAGES:
        raise ValueError(f"이미지는 한 번에 {MAX_CHAT_IMAGES}개까지만 붙여넣을 수 있습니다.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    ext_by_mime = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
    for index, item in enumerate(attachments, start=1):
        if not isinstance(item, dict):
            raise ValueError("첨부 이미지 형식이 올바르지 않습니다.")
        mime = str(item.get("mime") or item.get("type") or "")
        data_url = str(item.get("dataUrl") or "")
        if mime not in ext_by_mime or not data_url.startswith(f"data:{mime};base64,"):
            raise ValueError("png/jpeg/webp/gif 이미지만 지원합니다.")
        raw_b64 = data_url.split(",", 1)[1]
        try:
            binary = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("이미지 데이터를 읽지 못했습니다.")
        if len(binary) > MAX_CHAT_IMAGE_BYTES:
            raise ValueError("이미지는 파일당 8MB 이하만 붙여넣을 수 있습니다.")
        ext = ext_by_mime[mime]
        safe_agent = ''.join(ch for ch in agent_id if ch.isalnum() or ch in {'-', '_'})[:40] or 'agent'
        path = UPLOAD_DIR / f"{int(time.time()*1000)}-{safe_agent}-{request_id[:8]}-{index}.{ext}"
        path.write_bytes(binary)
        saved.append({"path": str(path), "mime": mime, "size": len(binary), "name": item.get("name") or path.name})
    return saved


def build_message_with_attachments(message: str, saved: list[dict[str, Any]]) -> str:
    text = message.strip()
    if not saved:
        return text
    lines = [text] if text else ["이미지를 첨부합니다."]
    lines.append("")
    lines.append("첨부 이미지 파일:")
    for item in saved:
        lines.append(f"- {item['path']} ({item['mime']}, {item['size']} bytes)")
    lines.append("위 로컬 이미지 파일을 필요하면 read 도구로 열어 확인하세요.")
    return "\n".join(lines)


def normalized_chat_text(message: str) -> str:
    return "".join(ch for ch in str(message or "").lower() if ch.isalnum() or "가" <= ch <= "힣")


def is_simple_ping_or_test(message: str, attachments: list[dict[str, Any]] | None = None) -> bool:
    text = str(message or "").strip()
    normalized = normalized_chat_text(text)
    simple = {
        "야", "뭐해", "뭐함", "있어", "듣고있어", "대답해", "테스트", "test",
        "ping", "핑", "확인", "응답해", "뭐야", "왜", "왜그래", "이미지테스트",
        "안녕", "안녕하세요", "ㅎㅇ", "하이", "hello", "hi",
    }
    return normalized in simple or (not attachments and text.endswith("?") and len(normalized) <= 10)


def quick_ping_reply(agent_name: str, message: str, attachments: list[dict[str, Any]] | None = None) -> str | None:
    if attachments and not is_simple_ping_or_test(message, attachments):
        return None
    if is_simple_ping_or_test(message, attachments):
        return f"네, 기선님. {agent_name} 듣고 있습니다."
    return None


def format_harness_time(ts: int) -> str:
    if not ts:
        return "시간 미상"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000))


def compact_report_line(text: str, limit: int = 220) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def recent_visible_reports_for_harness(agent_id: str, before_ms: int, limit: int = 3) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for item in reversed(load_history().get(agent_id, [])):
        if item.get("role") != "assistant":
            continue
        if item.get("status") not in {"done", "error", "synced"}:
            continue
        ts = int(item.get("ts") or 0)
        if before_ms and ts >= before_ms:
            continue
        text = strip_internal_report_contract(str(item.get("content") or ""))
        if is_internal_status_text(text):
            continue
        reports.append({"ts": ts, "text": text, "status": item.get("status")})
        if len(reports) >= limit:
            break
    return reports


def latest_work_item_for_harness(agent_id: str) -> dict[str, Any] | None:
    items = load_work_state().get(agent_id, [])
    if not isinstance(items, list):
        return None
    candidates = [item for item in items if isinstance(item, dict)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("updatedAt") or item.get("startedAt") or 0))


def chat_session_row_for_harness(agent_id: str, session_key: str) -> dict[str, Any] | None:
    try:
        data = json.loads(agent_session_store_path(agent_id).read_text())
    except Exception:
        return None
    row = data.get(session_key) if isinstance(data, dict) else None
    return row if isinstance(row, dict) else None


def build_harness_status_reply(agent_id: str, agent_name: str, session_key: str, message: str, request_start_ms: int, reason: str = "") -> str:
    reports = recent_visible_reports_for_harness(agent_id, request_start_ms, limit=3)
    work = latest_work_item_for_harness(agent_id)
    active = active_work_item(agent_id)
    session_row = chat_session_row_for_harness(agent_id, session_key)

    lines = [f"{agent_name} 상태를 관제 하네스가 대신 정리합니다."]
    if reason:
        lines.append(f"- 사유: 에이전트가 자연어 답변 대신 내부 상태값만 반환했습니다. ({reason})")
    if active:
        started = format_harness_time(int(active.get("startedAt") or 0))
        lines.append(f"- 현재 등록 작업: {str(active.get('status') or 'working')} · {started}")
        msg = compact_report_line(str(active.get("message") or ""), 180)
        if msg:
            lines.append(f"  - 요청: {msg}")
    elif work:
        updated = format_harness_time(int(work.get("updatedAt") or work.get("reportedAt") or work.get("startedAt") or 0))
        lines.append(f"- 최근 등록 작업: {str(work.get('status') or 'unknown')} · {updated}")
        msg = compact_report_line(str(work.get("message") or ""), 180)
        if msg:
            lines.append(f"  - 요청: {msg}")
    else:
        lines.append("- 현재 등록된 작업은 없습니다.")

    if session_row:
        updated = format_harness_time(to_epoch_ms(session_row.get("updatedAt") or session_row.get("startedAt")))
        lines.append(f"- 채팅 세션: {session_row.get('status') or 'unknown'} · 마지막 갱신 {updated}")

    if reports:
        lines.append("- 최근 가시 보고:")
        for report in reports:
            lines.append(f"  - {format_harness_time(int(report.get('ts') or 0))}: {compact_report_line(str(report.get('text') or ''))}")
    else:
        lines.append("- 최근 가시 완료보고는 찾지 못했습니다.")

    lines.append("- 조치: 내부 무응답 토큰은 숨기고, 이런 상태 질문에는 이 하네스 요약을 사용자에게 보이게 합니다.")
    return "\n".join(lines)


def is_work_request_message(message: str, attachments: list[dict[str, Any]] | None = None) -> bool:
    if is_simple_ping_or_test(message, attachments):
        return False
    text = str(message or "").strip().lower()
    normalized = normalized_chat_text(text)
    if (normalized.endswith("뭐야") or normalized.endswith("뭐지") or normalized.endswith("뭐니")) and not any(token in normalized for token in ("해줘", "고쳐", "수정", "구현", "만들")):
        return False
    work_markers = (
        "해줘", "해봐", "하자", "고쳐", "수정", "구현", "만들", "추가",
        "삭제", "정리", "조사", "찾아", "확인해", "분석", "리뷰", "감사",
        "audit", "fix", "implement", "refactor", "review", "test", "검증",
        "커밋", "푸시", "배포", "실행", "적용", "바꿔", "개선", "점검",
    )
    if any(marker in normalized or marker in text for marker in work_markers):
        return True
    if attachments and text and not is_simple_ping_or_test(message, attachments):
        return True
    return False


def complete_chat_async(agent_id: str, agent_name: str, session_key: str, message: str, request_id: str) -> None:
    """Send a chat turn to the selected OpenClaw agent and write the real final reply.

    Important: this bridge must not replace agent replies with local status macros.
    The visible chat row starts as pending, then becomes either the agent's final
    text or a concrete transport error.
    """
    args = [
        OPENCLAW_BIN,
        "gateway",
        "call",
        "sessions.send",
        "--expect-final",
        "--json",
        "--timeout",
        str(900_000),
        "--params",
        json.dumps({"key": session_key, "message": message}, ensure_ascii=False),
    ]
    env = os.environ.copy()
    env.pop("OPENCLAW_GATEWAY_URL", None)
    env.pop("OPENCLAW_API_URL", None)
    env["NO_COLOR"] = "1"
    request_start_ms = int(time.time() * 1000) - 1000
    try:
        result = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=920)
        raw = (result.stdout or result.stderr or "").strip()
        if result.returncode != 0:
            if "session not found" in raw.lower():
                fresh_key = observer_chat_session_key(agent_id)
                if fresh_key != session_key:
                    complete_chat_async(agent_id, agent_name, fresh_key, message, request_id)
                    return
            raise RuntimeError(raw or f"sessions.send failed with code {result.returncode}")
        payload = parse_json_suffix(raw)
        text = None
        if is_transport_status_payload(payload):
            text = wait_for_agent_reply(session_key, request_start_ms, timeout_seconds=900)
        if not text:
            text = extract_response_text(payload)
        if not text or text in {"{}", "[]"} or is_internal_status_text(text):
            text = wait_for_agent_reply(session_key, request_start_ms, timeout_seconds=120)
        if text:
            text = strip_internal_report_contract(text)
        if text and is_progress_only_report_text(text):
            final_text = wait_for_non_progress_agent_reply(session_key, request_start_ms, timeout_seconds=900)
            if final_text:
                text = strip_internal_report_contract(final_text)
        if not text or is_internal_status_text(text):
            reason = compact_report_line(text or "empty", 120)
            text = build_harness_status_reply(agent_id, agent_name, session_key, message, request_start_ms, reason)
        replace_history_message(
            agent_id,
            request_id,
            "system",
            text,
            {"status": "done", "done": True, "pending": False, "sessionKey": session_key, "role": "assistant"},
        )
        if is_progress_only_report_text(text):
            update_work_item(agent_id, request_id, "reporting", report=True)
        elif text.startswith("실패:") or "실패했습니다" in text or "중단했습니다" in text or "못했습니다" in text:
            update_work_item(agent_id, request_id, "failed", report=True, reason=text)
        else:
            update_work_item(agent_id, request_id, "reported", report=True)
            close_latest_active_work_from_report(agent_id, session_key, text)
    except Exception as exc:
        replace_history_message(
            agent_id,
            request_id,
            "system",
            f"실패: 에이전트 최종 응답을 받지 못했습니다. {exc}",
            {"status": "error", "error": True, "done": True, "pending": False, "sessionKey": session_key},
        )
        update_work_item(agent_id, request_id, "failed", report=True, reason=str(exc))


def cached_sessions_if_fresh() -> list[dict[str, Any]]:
    cached = cache_get("sessions")
    if not cache_is_fresh(cached):
        return []
    sessions = cached.get("sessions") if isinstance(cached, dict) else []
    return sessions if isinstance(sessions, list) else []


def fast_chat_session_for_send(agent_id: str) -> tuple[str, str | None]:
    # 채팅 UI 응답성을 위해 전송 요청 경로에서는 gateway 조회를 하지 않는다.
    # 신선한 세션 캐시가 있을 때만 롤오버 판단을 하고, 없으면 현재/기본 채팅키로
    # 즉시 pending을 반환한 뒤 백그라운드 sessions.send가 실제 전송을 처리한다.
    sessions = cached_sessions_if_fresh()
    if sessions:
        return maybe_rollover_chat_session_from_sessions(agent_id, sessions)
    return desired_observer_chat_session_key(agent_id), None


def send_chat(agent_id: str, message: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    validate_chat_message(agent_id, message, attachments)
    agent = next(a for a in AGENTS if a["id"] == agent_id)
    session_key, handoff_summary = fast_chat_session_for_send(agent_id)
    request_id = str(uuid.uuid4())
    saved_attachments = save_chat_attachments(agent_id, request_id, attachments)
    outbound_message = build_message_with_attachments(message, saved_attachments)
    if handoff_summary:
        outbound_message = f"{handoff_summary}\n\n[새 요청]\n{outbound_message}"
    display_message = message.strip() or "이미지 첨부"
    if saved_attachments:
        display_message += "\n" + "\n".join(f"[이미지] {item['path']}" for item in saved_attachments)
    append_history(agent_id, "user", display_message, {"requestId": request_id, "sessionKey": session_key, "attachments": saved_attachments})
    quick_reply = quick_ping_reply(agent["name"], message, saved_attachments)
    if quick_reply:
        append_history(agent_id, "assistant", quick_reply, {"requestId": request_id, "sessionKey": session_key, "status": "done", "done": True, "pending": False, "quickAck": True})
        history_payload = public_history(agent_id, mark_read=False)
        return {"ok": True, "accepted": True, "pending": False, "quickAck": True, "requestId": request_id, "agentId": agent_id, "agentName": agent["name"], "sessionKey": session_key, "attachments": saved_attachments, "history": history_payload.get("messages", [])}
    append_history(agent_id, "system", "전달됨. 응답을 기다리는 중입니다...", {"requestId": request_id, "sessionKey": session_key, "status": "pending", "pending": True})
    if is_work_request_message(message, saved_attachments):
        register_work_item(agent_id, request_id, outbound_message, session_key)
    report_contract = (
        "\n\n[관제 보고 규칙] "
        "작업 요청이면 시작보고와 완료보고를 구분하세요. "
        "'시작합니다/진행하겠습니다/완료되면 보고하겠습니다'는 완료가 아닙니다. "
        "최종 산출물, 검증 결과, 실패/차단 사유를 사용자에게 보고해야 일이 완료됩니다. "
        "이전 관제 대화 요약/최근 완료보고 후보가 포함된 새 세션 이어받기에서는 세션이 바뀌었더라도 같은 대화로 취급하세요. "
        "사용자가 완료보고가 안 보인다고 하거나 확인 결과를 요구하면, 이전 세션 탓만 하지 말고 현재 채팅에 완료보고를 즉시 재게시하세요."
    )
    outbound_for_worker = f"{outbound_message}{report_contract}"
    worker = threading.Thread(target=complete_chat_async, args=(agent_id, agent["name"], session_key, outbound_for_worker, request_id), daemon=True)
    worker.start()
    history_payload = public_history(agent_id, mark_read=False)
    return {"ok": True, "accepted": True, "pending": True, "requestId": request_id, "agentId": agent_id, "agentName": agent["name"], "sessionKey": session_key, "attachments": saved_attachments, "history": history_payload.get("messages", [])}


def send_recovery_nudge(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    agent = next(a for a in AGENTS if a["id"] == agent_id)
    session_key = observer_chat_session_key(agent_id)
    message = (
        "관제 화면에서 현재 작업 중 상태로 표시됩니다. "
        "지금 실제 상태를 확인해서 짧게 보고하세요. "
        "반드시 현재 실행 중인지/막혔는지, 마지막으로 확인한 파일·로그·세션·작업 단위, 다음 조치를 포함하세요. "
        "게이트웨이/systemd/config 변경은 하지 마세요."
    )
    args = [
        OPENCLAW_BIN,
        "gateway",
        "call",
        "sessions.send",
        "--json",
        "--timeout",
        "20000",
        "--params",
        json.dumps({"key": session_key, "message": message}, ensure_ascii=False),
    ]
    env = os.environ.copy()
    env.pop("OPENCLAW_GATEWAY_URL", None)
    env.pop("OPENCLAW_API_URL", None)
    env["NO_COLOR"] = "1"
    result = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=28)
    raw = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(raw or "상태 재확인 요청 실패")
    payload = parse_json_suffix(raw)
    append_history(agent_id, "system", "관제 화면에서 상태 재확인 요청을 보냈습니다.", {"status": "recovery-requested", "sessionKey": session_key})
    return {"ok": True, "agentId": agent_id, "agentName": agent["name"], "sessionKey": session_key, "result": payload, "history": load_history().get(agent_id, [])[-80:]}


def load_nudges() -> dict[str, Any]:
    try:
        data = json.loads(NUDGE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_nudges(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    NUDGE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def send_silence_nudge(agent_id: str, request_id: str | None = None) -> None:
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        return
    session_key = observer_chat_session_key(agent_id)
    nudge_request_id = f"silence-{request_id or str(uuid.uuid4())}"
    append_history(agent_id, "system", "시스템: 무응답 경고 · 상태보고 요청 중", {"status": "pending", "pending": True, "autoNudge": True, "nudgeKind": "silence", "sessionKey": session_key, "requestId": nudge_request_id})
    message = (
        "상태 보고 요청: 2분 이상 관제 웹에 가시 보고가 없습니다. "
        "현재 상태를 짧게 보고하세요. 작업 중이면 진행률/현재 단계/막힌 점/다음 조치, "
        "완료했다면 최종 산출물과 검증 결과를 관제 웹 채팅에 보고하세요. "
        "이 요청에는 반드시 사용자에게 보이는 자연어 답변으로 응답하세요."
    )
    complete_chat_async(agent_id, agent["name"], session_key, message, nudge_request_id)


def send_force_check(agent_id: str) -> dict[str, Any]:
    agent = next((a for a in AGENTS if a["id"] == agent_id), None)
    if not agent:
        raise ValueError("허용되지 않은 에이전트입니다.")
    session_key = observer_chat_session_key(agent_id)
    request_id = f"force-check-{uuid.uuid4()}"
    append_history(agent_id, "system", "시스템: 수동 깨우기 · activeWork/하네스 회수 점검 요청 중", {"status": "pending", "pending": True, "manualWake": True, "sessionKey": session_key, "requestId": request_id})
    message = (
        "관제 웹에서 수동 깨우기/회수 점검을 요청했습니다. "
        "먼저 activeWork, 실행 중인 subagent/background process, 최근 로그, git status를 실제 확인하세요. "
        "하네스/자율 작업이 completed인데 관제 webchat 최종 보고가 없으면 새 작업을 시작하지 말고 누락 보고를 복구하세요. "
        "보고에는 현재 단계, 완료한 것, 검증/QA/외부 링크 또는 artifact, 커밋, 남은 실패/차단, 다음 행동을 포함하세요. "
        "Gateway/systemd/live config는 건드리지 마세요. 이 요청에는 반드시 사용자에게 보이는 자연어 답변으로 응답하세요."
    )
    threading.Thread(target=complete_chat_async, args=(agent_id, agent["name"], session_key, message, request_id), daemon=True).start()
    return {"ok": True, "agentId": agent_id, "agentName": agent["name"], "sessionKey": session_key, "requestId": request_id, "history": load_history().get(agent_id, [])[-80:]}


def maybe_auto_nudge(agent: dict[str, Any]) -> None:
    agent_id = str(agent.get("id") or "")
    if not agent_id:
        return
    now = int(time.time())
    nudges = load_nudges()
    if agent.get("isLagging"):
        key = f"lag:{agent_id}"
        last = int(nudges.get(key, 0) or 0)
        if now - last >= NUDGE_INTERVAL_SECONDS:
            nudges[key] = now
            save_nudges(nudges)
            threading.Thread(target=lambda: send_recovery_nudge(agent_id), daemon=True).start()
    self_work = agent.get("selfWork") if isinstance(agent.get("selfWork"), dict) else {}
    if agent.get("selfWorkReportDue") and self_work:
        request_id = str(self_work.get("requestId") or "")
        task = str(self_work.get("message") or "후타바 직접 작업").strip()[:80]
        key = f"self-work:{agent_id}:{request_id or 'active'}"
        last = int(nudges.get(key, 0) or 0)
        # 같은 미완료 self-work가 1분마다 채팅을 도배하지 않도록 일반 무음 경고와
        # 같은 15분 쿨다운을 사용한다. 카드/상세 상태는 계속 표시하되 채팅에는
        # 작업명과 requestId가 들어간 이벤트만 남긴다.
        if now - last >= SILENCE_NUDGE_INTERVAL_SECONDS:
            nudges[key] = now
            save_nudges(nudges)
            append_history(agent_id, "system", f"시스템: 후타바 직접 작업 보고 기한 초과 · {task} · 중간/완료 보고 필요", {"status": "self-work-report-due", "sessionKey": observer_chat_session_key(agent_id), "requestId": request_id})
    if agent.get("needsSilenceReport"):
        if not SILENCE_AUTO_NUDGE_ENABLED:
            return
        work = agent.get("activeWork") if isinstance(agent.get("activeWork"), dict) else {}
        request_id = str(work.get("requestId") or "")
        if not request_id and not agent.get("reportDebt"):
            return
        key = f"silence:{agent_id}:{request_id or 'report-debt'}"
        last = int(nudges.get(key, 0) or 0)
        if now - last >= SILENCE_NUDGE_INTERVAL_SECONDS:
            nudges[key] = now
            save_nudges(nudges)
            if request_id:
                mark_work_nudged(agent_id, request_id)
            threading.Thread(target=lambda: send_silence_nudge(agent_id, request_id or None), daemon=True).start()


def get_agent_session_detail(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    summary = summarize_agents()
    agent = next((a for a in summary.get("agents", []) if a.get("id") == agent_id), None)

    # Detail button lives inside the operator chat. When the agent is idle, the
    # latest gateway session is often a heartbeat session (for example
    # agent:main:main) and shows HEARTBEAT_OK/tool logs instead of the work the
    # operator just discussed. Show live runtime logs only while the agent is
    # actually working; otherwise show the dedicated observer-site chat history.
    runtime_key = str(agent.get("latestSessionKey") or "") if agent else ""
    use_runtime_log = bool(agent and agent.get("isWorkingNow") and runtime_key)
    key = runtime_key if use_runtime_log else observer_chat_session_key(agent_id)
    source = "runtime" if use_runtime_log else "observer-chat"

    compact_messages = []
    if use_runtime_log:
        messages_payload = gateway_call("sessions.get", {"key": key, "limit": 20}, timeout=18)
        messages = messages_payload.get("messages") if isinstance(messages_payload, dict) else []
        if not isinstance(messages, list):
            messages = []
        for msg in messages[-20:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or msg.get("type") or "message"
            content = msg.get("content") or msg.get("text") or msg.get("message") or ""
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)[:2000]
            compact_messages.append({"role": role, "content": strip_internal_report_contract(content)[:2000], "ts": msg.get("ts") or msg.get("timestamp") or msg.get("createdAt")})
    else:
        for msg in public_history(agent_id, mark_read=False).get("messages", [])[-20:]:
            if not isinstance(msg, dict):
                continue
            compact_messages.append({
                "role": msg.get("role") or "message",
                "content": strip_internal_report_contract(str(msg.get("content") or ""))[:2000],
                "ts": msg.get("ts") or msg.get("timestamp") or msg.get("createdAt"),
            })

    return {
        "ok": True,
        "agentId": agent_id,
        "sessionKey": key,
        "source": source,
        "runtimeSessionKey": runtime_key or None,
        "agent": agent,
        "messages": compact_messages,
    }


def abort_agent_session(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    summary = summarize_agents()
    agent = next((a for a in summary.get("agents", []) if a.get("id") == agent_id), None)
    key = agent.get("latestSessionKey") if agent else observer_chat_session_key(agent_id)
    result = gateway_call("sessions.abort", {"key": key}, timeout=18)
    append_history(agent_id, "system", f"관제 화면에서 세션 중단을 요청했습니다: {key}", {"status": "abort-requested", "sessionKey": key})
    return {"ok": True, "agentId": agent_id, "sessionKey": key, "result": result, "history": load_history().get(agent_id, [])[-80:]}


def abort_orphan_subagent_sessions(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    now = int(time.time() * 1000)
    sessions = get_sessions_cached(timeout=18)
    candidates = []
    for session in sessions:
        key = str(session.get("key") or "")
        if ":subagent:" not in key and not session.get("spawnedBy"):
            continue
        if subagent_owner_id(session) != agent_id:
            continue
        status = str(session.get("status") or "")
        if normalize_status(status) != "working":
            continue
        updated = to_epoch_ms(session.get("updatedAt") or session.get("startedAt"))
        stale_ms = max(0, now - updated) if updated else ORPHAN_RUNNING_CANDIDATE_MS + 1
        if stale_ms < ORPHAN_RUNNING_CANDIDATE_MS:
            continue
        candidates.append({"key": key, "status": status, "staleSeconds": stale_ms // 1000})
    results = []
    for item in candidates:
        key = str(item.get("key") or "")
        if not key:
            continue
        try:
            result = gateway_call("sessions.abort", {"key": key}, timeout=18)
            cleanup_state_set(agent_id, key, "resolved-orphan")
            results.append({"key": key, "ok": True, "result": result})
        except Exception as exc:
            results.append({"key": key, "ok": False, "error": str(exc)})
    append_history(
        agent_id,
        "system",
        f"고아 running 서브에이전트 중단 요청: 후보 {len(candidates)}개, 성공 {sum(1 for r in results if r.get('ok'))}개",
        {"status": "cleanup-orphans", "hidden": True, "results": results},
    )
    invalidate_agents_cache()
    return {"ok": True, "agentId": agent_id, "candidates": len(candidates), "aborted": sum(1 for r in results if r.get("ok")), "results": results}


def archive_subagent_candidate_sessions(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    now = int(time.time() * 1000)
    sessions = get_sessions_cached(timeout=18)
    keys = []
    for session in sessions:
        key = str(session.get("key") or "")
        if ":subagent:" not in key and not session.get("spawnedBy"):
            continue
        if subagent_owner_id(session) != agent_id:
            continue
        if cleanup_state_kind(agent_id, key) == "archived":
            continue
        status = str(session.get("status") or "")
        normalized = normalize_status(status)
        if normalized == "working":
            continue
        updated = to_epoch_ms(session.get("updatedAt") or session.get("startedAt"))
        stale_ms = max(0, now - updated) if updated else ARCHIVE_CANDIDATE_MS + 1
        if stale_ms >= ARCHIVE_CANDIDATE_MS:
            keys.append(key)
    store_path = agent_session_store_path(agent_id)
    if not store_path.exists():
        raise RuntimeError(f"session store not found: {store_path}")
    store = json.loads(store_path.read_text())
    if not isinstance(store, dict):
        raise RuntimeError("session store format is not an object")
    archive_entries = {key: store[key] for key in keys if key in store}
    if not archive_entries:
        return {"ok": True, "agentId": agent_id, "candidates": len(keys), "archived": 0, "archivePath": None}
    SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = SESSION_ARCHIVE_DIR / f"{stamp}-{agent_id}-sessions.json"
    backup_path = store_path.with_suffix(f".json.bak-{stamp}")
    backup_path.write_text(json.dumps(store, ensure_ascii=False, indent=2))
    archive_path.write_text(json.dumps({"agentId": agent_id, "archivedAt": int(time.time() * 1000), "entries": archive_entries}, ensure_ascii=False, indent=2))
    for key in archive_entries:
        store.pop(key, None)
        cleanup_state_set(agent_id, key, "archived")
    store_path.write_text(json.dumps(store, ensure_ascii=False, indent=2))
    append_history(agent_id, "system", f"보관 후보 세션 archive 완료: {len(archive_entries)}개", {"status": "archive-candidates", "hidden": True, "archivePath": str(archive_path), "backupPath": str(backup_path)})
    invalidate_agents_cache()
    return {"ok": True, "agentId": agent_id, "candidates": len(keys), "archived": len(archive_entries), "archivePath": str(archive_path), "backupPath": str(backup_path)}


def gateway_call(method: str, params: dict[str, Any] | None = None, timeout: int = 8) -> Any:
    args = [OPENCLAW_BIN, "gateway", "call", method, "--json", "--timeout", str(timeout * 1000)]
    if params is not None:
        args.extend(["--params", json.dumps(params, ensure_ascii=False)])
    env = os.environ.copy()
    env.pop("OPENCLAW_GATEWAY_URL", None)
    env.pop("OPENCLAW_API_URL", None)
    env["NO_COLOR"] = "1"
    result = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout + 4)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "gateway call failed").strip())
    return parse_json_suffix(result.stdout)


def to_epoch_ms(value: Any) -> int:
    try:
        num = float(value)
    except Exception:
        return 0
    if num <= 0:
        return 0
    return int(num * 1000 if num < 1_000_000_000_000 else num)


def normalize_status(raw: str | None) -> str:
    text = (raw or "").lower()
    if text in {"running", "processing", "queued"}:
        return "working"
    if text in {"failed", "error", "timed_out", "stuck"}:
        return "warning"
    return "idle"


def summarize_gateway_status() -> dict[str, Any]:
    args = [OPENCLAW_BIN, "gateway", "status"]
    env = os.environ.copy()
    env.pop("OPENCLAW_GATEWAY_URL", None)
    env.pop("OPENCLAW_API_URL", None)
    env["NO_COLOR"] = "1"
    generated = int(time.time() * 1000)
    started = time.time()
    result = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=14)
    elapsed_ms = int((time.time() - started) * 1000)
    raw = (result.stdout or result.stderr or "").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    runtime = next((line for line in lines if line.startswith("Runtime:")), "Runtime: unknown")
    probe = next((line for line in lines if line.startswith("Connectivity probe:")), "Connectivity probe: unknown")
    gateway = next((line for line in lines if line.startswith("Gateway:")), "Gateway: unknown")
    listening = next((line for line in lines if line.startswith("Listening:")), None)
    log_line = next((line for line in lines if line.startswith("File logs:")), None)
    ok = result.returncode == 0 and "running" in runtime.lower() and "ok" in probe.lower()
    return {
        "ok": ok,
        "state": "ok" if ok else "warning",
        "generatedAt": generated,
        "latencyMs": elapsed_ms,
        "runtime": runtime.replace("Runtime:", "", 1).strip(),
        "probe": probe.replace("Connectivity probe:", "", 1).strip(),
        "gateway": gateway.replace("Gateway:", "", 1).strip(),
        "listening": listening.replace("Listening:", "", 1).strip() if listening else None,
        "logs": log_line.replace("File logs:", "", 1).strip() if log_line else None,
        "raw": lines[:18],
    }


def subagent_owner_id(session: dict[str, Any]) -> str | None:
    key = str(session.get("key") or "")
    spawned = str(session.get("spawnedBy") or "")
    for value in (key, spawned):
        if value.startswith("agent:"):
            parts = value.split(":")
            if len(parts) >= 2:
                return parts[1]
    return None


def summarize_subagents(agent_id: str, sessions: list[dict[str, Any]], now: int) -> dict[str, Any]:
    subs = []
    for session in sessions:
        key = str(session.get("key") or "")
        if ":subagent:" not in key and not session.get("spawnedBy"):
            continue
        if subagent_owner_id(session) != agent_id:
            continue
        updated = to_epoch_ms(session.get("updatedAt") or session.get("startedAt"))
        stale_seconds = max(0, (now - updated) // 1000) if updated else 0
        status = str(session.get("status") or "unknown")
        normalized = normalize_status(status)
        state = "done"
        if normalized == "working":
            if stale_seconds * 1000 > SUBAGENT_ACTIVE_MAX_STALE_MS:
                state = "stale"
            else:
                state = "working"
        elif normalized == "warning":
            state = "failed"
        preview = session.get("lastMessagePreview") or session.get("title") or session.get("label") or ""
        if not isinstance(preview, str):
            preview = json.dumps(preview, ensure_ascii=False)[:500]
        tokens = int(float(session.get("totalTokens") or 0) or 0)
        cleanup_kind = None
        resolved_kind = cleanup_state_kind(agent_id, key)
        if resolved_kind in {"resolved-orphan", "archived"}:
            cleanup_kind = None
        elif state == "stale" and stale_seconds * 1000 >= ORPHAN_RUNNING_CANDIDATE_MS:
            cleanup_kind = "orphan-running"
        elif state in {"done", "failed"} and stale_seconds * 1000 >= ARCHIVE_CANDIDATE_MS:
            cleanup_kind = "archive-candidate"
        item = {
            "key": key,
            "state": state,
            "status": status,
            "staleSeconds": stale_seconds,
            "model": session.get("model"),
            "tokens": tokens,
            "preview": preview[:500],
            "cleanupKind": cleanup_kind,
        }
        subs.append(item)
    subs.sort(key=lambda item: item.get("staleSeconds", 0))
    visible = [item for item in subs if item["state"] == "working"]
    cleanup_candidates = [item for item in subs if item.get("cleanupKind")]
    recent = visible[:12]
    return {
        "total": len(visible),
        "hiddenTotal": max(0, len(subs) - len(recent)),
        "recent": recent,
        "done": sum(1 for item in subs if item["state"] == "done"),
        "working": sum(1 for item in visible if item["state"] == "working"),
        "lag": sum(1 for item in subs if item["state"] == "stale"),
        "failed": sum(1 for item in subs if item["state"] == "failed"),
        "cleanupCandidates": len(cleanup_candidates),
        "orphanRunningCandidates": sum(1 for item in cleanup_candidates if item.get("cleanupKind") == "orphan-running"),
        "archiveCandidates": sum(1 for item in cleanup_candidates if item.get("cleanupKind") == "archive-candidate"),
    }



def latest_visible_agent_report_ts(agent_id: str, sessions: list[dict[str, Any]] | None = None, chat_key: str | None = None) -> int:
    local_ts = terminal_reply_ts(load_history().get(agent_id, []))
    if sessions is None:
        return local_ts
    return max(local_ts, latest_session_assistant_report_ts(agent_id, sessions, chat_key))


def terminal_reply_ts(entries: list[dict[str, Any]]) -> int:
    latest = 0
    for item in entries:
        if item.get("role") != "assistant":
            continue
        if item.get("status") not in {"done", "error", "synced"}:
            continue
        content = str(item.get("content") or "")
        if is_internal_status_text(content):
            continue
        latest = max(latest, int(item.get("ts") or 0))
    return latest


def latest_pending_assignment_ms(agent_id: str, now: int) -> int:
    entries = load_history().get(agent_id, [])
    latest_terminal_ts = terminal_reply_ts(entries)
    latest = 0
    for item in entries:
        if item.get("role") != "system" or item.get("status") not in {"pending", "delayed-pending"}:
            continue
        if item.get("pending") is not True:
            continue
        ts = int(item.get("ts") or 0)
        if ts and ts > latest_terminal_ts:
            latest = max(latest, ts)
    return latest


def latest_unanswered_user_ms(agent_id: str) -> int:
    entries = load_history().get(agent_id, [])
    latest_terminal_ts = terminal_reply_ts(entries)
    latest_user_ts = 0
    for item in entries:
        if item.get("role") == "user":
            latest_user_ts = max(latest_user_ts, int(item.get("ts") or 0))
    return latest_user_ts if latest_user_ts > latest_terminal_ts else 0


def latest_report_pending_ms(agent_id: str) -> int:
    entries = load_history().get(agent_id, [])
    latest_user_ts = 0
    latest_progress_ts = 0
    latest_terminal_ts = 0
    for item in entries:
        ts = int(item.get("ts") or 0)
        if item.get("role") == "user":
            latest_user_ts = max(latest_user_ts, ts)
    if not latest_user_ts:
        return 0
    latest_user_text = ""
    for item in entries:
        if item.get("role") == "user" and int(item.get("ts") or 0) == latest_user_ts:
            latest_user_text = str(item.get("content") or "")
            break
    if is_simple_ping_or_test(latest_user_text):
        return 0
    for item in entries:
        ts = int(item.get("ts") or 0)
        if ts <= latest_user_ts or item.get("role") != "assistant" or item.get("status") not in {"done", "error", "synced"}:
            continue
        content = str(item.get("content") or "")
        if is_internal_status_text(content):
            continue
        if is_progress_only_report_text(content):
            latest_progress_ts = max(latest_progress_ts, ts)
        else:
            latest_terminal_ts = max(latest_terminal_ts, ts)
    return latest_user_ts if latest_progress_ts and latest_progress_ts > latest_terminal_ts else 0


def expire_stale_pending_assignments(now: int | None = None) -> int:
    # Keep genuinely unresolved questions as pending/delayed, but clear pending
    # rows once a later assistant done/error exists. Otherwise finished chats can
    # stay stuck as "응답 지연" for hours.
    now = now or int(time.time() * 1000)
    history = load_history()
    changed = 0
    for entries in history.values():
        latest_terminal_ts = terminal_reply_ts(entries)
        for item in entries:
            if item.get("role") != "system" or item.get("status") not in {"pending", "delayed-pending"}:
                continue
            ts = int(item.get("ts") or 0)
            if ts and ts <= latest_terminal_ts:
                item["status"] = "resolved-pending"
                item["pending"] = False
                item["hidden"] = True
                changed += 1
            elif item.get("status") == "pending" and ts and now - ts > ASSIGNED_MAX_AGE_MS:
                item["status"] = "delayed-pending"
                item["pending"] = True
                item.pop("hidden", None)
                changed += 1
    if changed:
        save_history(history)
    return changed



def sanitize_internal_report_contract_messages() -> int:
    history = load_history()
    changed = 0
    for entries in history.values():
        for item in entries:
            content = str(item.get("content") or "")
            if "[관제 보고 규칙]" not in content:
                continue
            cleaned = strip_internal_report_contract(content)
            if cleaned:
                item["content"] = cleaned
            else:
                item["hidden"] = True
                item["status"] = "internal-status"
            changed += 1
    if changed:
        save_history(history)
        invalidate_agents_cache()
    return changed

def sanitize_internal_done_messages() -> int:
    history = load_history()
    changed = 0
    for agent_id, entries in history.items():
        agent = next((base for base in AGENTS if base["id"] == agent_id), None)
        agent_name = apply_agent_settings(agent).get("name") if agent else agent_id
        for item in entries:
            if item.get("role") != "assistant" or item.get("status") not in {"done", "internal-status"}:
                continue
            content = str(item.get("content") or "")
            if not is_internal_status_text(content):
                continue
            request_id = str(item.get("requestId") or "")
            if content.strip() in {"NO_REPLY", "HEARTBEAT_OK"} and request_id:
                request_ts = 0
                for peer in entries:
                    if peer.get("requestId") == request_id and peer.get("role") == "user":
                        request_ts = int(peer.get("ts") or 0)
                        break
                item["content"] = build_harness_status_reply(agent_id, str(agent_name or agent_id), str(item.get("sessionKey") or observer_chat_session_key(agent_id)), "", request_ts or int(item.get("ts") or 0), content.strip())
                item["status"] = "done"
                item["harnessFallback"] = True
                item["done"] = True
                item["pending"] = False
                item.pop("hidden", None)
            else:
                item["status"] = "internal-status"
                item["hidden"] = True
                item["pending"] = False
            changed += 1
    if changed:
        save_history(history)
    return changed

def latest_history_work_evidence_ms(agent_id: str) -> tuple[int, str | None]:
    evidence_statuses = {"cleanup-orphans", "archive-candidates", "session-rollover"}
    latest_ts = 0
    reason = None
    for item in load_history().get(agent_id, []):
        if item.get("role") != "system" or item.get("status") not in evidence_statuses:
            continue
        ts = int(item.get("ts") or 0)
        if ts > latest_ts:
            latest_ts = ts
            reason = str(item.get("status") or "작업 증거")
    return latest_ts, reason


def latest_observer_git_evidence_ms() -> tuple[int, str | None]:
    try:
        result = subprocess.run(["git", "log", "-1", "--format=%ct %s"], cwd=str(ROOT), capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return 0, None
        raw = result.stdout.strip()
        if not raw:
            return 0, None
        sec, _, subject = raw.partition(" ")
        return int(float(sec) * 1000), f"git commit: {subject[:80]}"
    except Exception:
        return 0, None


def latest_subagent_done_evidence_ms(agent_id: str, sessions: list[dict[str, Any]]) -> tuple[int, str | None]:
    latest_ts = 0
    reason = None
    for session in sessions:
        if not isinstance(session, dict):
            continue
        key = str(session.get("key") or "")
        if ":subagent:" not in key and not session.get("spawnedBy"):
            continue
        if subagent_owner_id(session) != agent_id:
            continue
        normalized = normalize_status(str(session.get("status") or ""))
        if normalized == "working":
            continue
        ts = to_epoch_ms(session.get("updatedAt") or session.get("endedAt") or session.get("startedAt"))
        if ts > latest_ts:
            latest_ts = ts
            reason = f"subagent {str(session.get('status') or 'done')}: {key[:72]}"
    return latest_ts, reason


def load_report_ts_cache() -> dict[str, Any]:
    try:
        return json.loads(REPORT_TS_CACHE_PATH.read_text())
    except Exception:
        return {}


def save_report_ts_cache(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_TS_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def session_cache_key(session: dict[str, Any]) -> str:
    return f"{session.get('sessionFile') or ''}|{session.get('updatedAt') or ''}|{session.get('status') or ''}"


def latest_session_assistant_report_ts(agent_id: str, sessions: list[dict[str, Any]], chat_key: str | None = None) -> int:
    keys = {chat_key or observer_chat_session_key_from_rows(agent_id, agent_session_key_rows(agent_id, sessions)), base_observer_chat_session_key(agent_id)}
    latest_ts = 0
    cache = load_report_ts_cache()
    changed = False
    for session in sessions:
        if not isinstance(session, dict):
            continue
        key = str(session.get("key") or "")
        if key not in keys:
            continue
        session_file = session.get("sessionFile")
        if not session_file:
            continue
        path = Path(str(session_file))
        if not path.is_file():
            continue
        cache_key = session_cache_key(session)
        cached = cache.get(cache_key) if isinstance(cache, dict) else None
        if isinstance(cached, int):
            latest_ts = max(latest_ts, cached)
            continue
        session_latest = 0
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 1_000_000))
                chunk = handle.read().decode("utf-8", errors="replace")
            lines = chunk.splitlines()[-500:]
        except Exception:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            message = row.get("message") if isinstance(row, dict) else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = extract_content_text(message.get("content"))
            if not text or is_internal_status_text(text) or is_progress_only_report_text(text):
                continue
            ts = to_epoch_ms(row.get("timestamp"))
            session_latest = max(session_latest, ts)
        cache[cache_key] = session_latest
        changed = True
        latest_ts = max(latest_ts, session_latest)
    if changed:
        try:
            save_report_ts_cache(dict(list(cache.items())[-300:]))
        except Exception:
            pass
    return latest_ts


def latest_report_debt(agent_id: str, sessions: list[dict[str, Any]], chat_key: str | None = None) -> dict[str, Any] | None:
    active = active_work_item(agent_id)
    if not active:
        return None
    started = int(active.get("startedAt") or 0)
    last_report_ts = latest_visible_agent_report_ts(agent_id, sessions, chat_key)
    candidates: list[tuple[int, str]] = []
    h_ts, h_reason = latest_history_work_evidence_ms(agent_id)
    if h_ts and h_reason:
        candidates.append((h_ts, h_reason))
    s_ts, s_reason = latest_subagent_done_evidence_ms(agent_id, sessions)
    if s_ts and s_reason:
        candidates.append((s_ts, s_reason))
    if agent_id == "observer":
        g_ts, g_reason = latest_observer_git_evidence_ms()
        if g_ts and g_reason:
            candidates.append((g_ts, g_reason))
    candidates = [(ts, reason) for ts, reason in candidates if ts >= started]
    if not candidates:
        return None
    evidence_ts, reason = max(candidates, key=lambda item: item[0])
    if evidence_ts <= last_report_ts:
        return None
    return {"ts": evidence_ts, "reason": reason, "lastReportTs": last_report_ts}


def load_mylene_active_work() -> dict[str, Any] | None:
    try:
        data = json.loads(MYLENE_HEARTBEAT_STATE_PATH.read_text())
        active = data.get("activeWork") if isinstance(data, dict) else None
        return active if isinstance(active, dict) else None
    except Exception:
        return None


def tail_text(path: str, max_bytes: int = 12_000) -> str:
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return ""
        with p.open("rb") as fh:
            try:
                fh.seek(max(0, p.stat().st_size - max_bytes))
            except Exception:
                pass
            return fh.read(max_bytes).decode("utf-8", "replace")
    except Exception:
        return ""


def process_matches_hint(hint: str) -> bool:
    if not hint:
        return False
    try:
        result = subprocess.run(["pgrep", "-af", hint], capture_output=True, text=True, timeout=2)
        return result.returncode == 0 and bool((result.stdout or "").strip())
    except Exception:
        return False


def cpu_hog_alerts() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(["ps", "-eo", "pid,etimes,pcpu,pmem,comm,args", "--sort=-pcpu"], capture_output=True, text=True, timeout=3)
    except Exception:
        return []
    alerts: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines()[1:12]:
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        pid, etimes, cpu, mem, comm, args = parts
        try:
            cpu_f = float(cpu)
            age_s = int(float(etimes))
        except Exception:
            continue
        if cpu_f < 120:
            continue
        if not any(token in args for token in ["chrome-headless", "playwright", "qa-", "vite", "openclaw-gateway"]):
            continue
        if "openclaw-gateway" in args and age_s < 30 and cpu_f < 220:
            continue
        if "openclaw-gateway" not in args and not any(token in args for token in ["chrome-headless", "playwright", "qa-", "vite"]):
            continue
        alerts.append({"kind": "cpu-hog", "severity": "warning" if cpu_f < 250 else "critical", "title": f"CPU 폭주 후보 {comm} {cpu_f:.0f}%", "detail": f"pid={pid} age={age_s}s mem={mem}% cmd={args[:180]}", "pid": pid, "cpu": cpu_f})
    return alerts


def active_work_log_has_terminal_marker(text: str) -> bool:
    if not text:
        return False
    markers = (
        "== status ==",
        "Process exited with code 0",
        "✓ built",
        "vite_build_noempty=0",
        "restore_dist=0",
        "diff_check=0",
        "tsc_noemit=0",
    )
    return any(marker in text for marker in markers)


def active_work_alerts() -> list[dict[str, Any]]:
    active = load_mylene_active_work()
    if not active:
        return []
    alerts: list[dict[str, Any]] = []
    status = str(active.get("status") or "")
    title = str(active.get("title") or active.get("id") or "activeWork")
    units = active.get("executionUnits") if isinstance(active.get("executionUnits"), list) else []
    running_units = [u for u in units if isinstance(u, dict) and u.get("status") == "running"]
    for unit in running_units:
        log = str(unit.get("log") or "")
        command = str(unit.get("command") or "")
        pid = str(unit.get("pid") or "")
        alive = bool(pid and Path(f"/proc/{pid}").exists()) or process_matches_hint(command[:80])
        text = tail_text(log) if log else ""
        if not alive and not active_work_log_has_terminal_marker(text):
            alerts.append({"kind": "active-work-orphan", "severity": "critical", "title": "activeWork running인데 실행 단위가 안 보임", "detail": f"{title} · unit={unit.get('label') or unit.get('type')} · log={log or '-'}"})
        lowered = text.lower()
        if text and any(key in lowered for key in ["qa_report_artifacts=1", "publication_semantic=2", "semantic_static=2", "error:", "traceback"]):
            alerts.append({"kind": "active-work-log-failure", "severity": "critical", "title": "activeWork 로그 실패 감지", "detail": f"{title} · {unit.get('label') or unit.get('type')} · {log}"})
    if status == "completed":
        last_report = str(active.get("lastUserReportAt") or "")
        completed = str(active.get("completedAt") or "")
        if completed and (not last_report or last_report < completed):
            alerts.append({"kind": "completed-report-missing", "severity": "critical", "title": "completed 후 사용자 보고 누락 가능", "detail": f"{title} · completedAt={completed} · lastUserReportAt={last_report or '-'}"})
    return alerts


def summarize_ops_alerts() -> dict[str, Any]:
    alerts = (active_work_alerts() + cpu_hog_alerts())[:12]
    return {"total": len(alerts), "critical": sum(1 for a in alerts if a.get("severity") == "critical"), "warning": sum(1 for a in alerts if a.get("severity") == "warning"), "alerts": alerts}


def is_nonblocking_maintenance_session(session: dict[str, Any]) -> bool:
    key = str(session.get("key") or "")
    label = str(session.get("label") or session.get("displayName") or session.get("derivedTitle") or "")
    preview = str(session.get("lastMessagePreview") or "")
    if "content-report-intraday-snapshot-refresh" in key or "content-report-intraday-snapshot-refresh" in label:
        return True
    if preview.strip() == "CONTENT_REPORT_REFRESH_OK":
        return True
    return False


def parse_iso_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def summarize_acp_sessions(limit: int = 8) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in ACP_SESSION_DIRS:
        if not directory.exists():
            continue
        for path in directory.glob("*acp*.json"):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            if data.get("schema") != "acpx.session.v1" and "acp_session_id" not in data:
                continue
            record_id = str(data.get("acpx_record_id") or path.stem)
            if record_id in seen:
                continue
            seen.add(record_id)
            raw_pid = data.get("pid")
            pid = int(raw_pid) if str(raw_pid or "").isdigit() else 0
            pid_alive = bool(pid and Path(f"/proc/{pid}").exists())
            closed = bool(data.get("closed") or data.get("closed_at"))
            updated_ms = parse_iso_ms(data.get("updated_at") or data.get("last_used_at") or data.get("created_at"))
            created_ms = parse_iso_ms(data.get("created_at"))
            messages = data.get("messages") if isinstance(data.get("messages"), list) else []
            title = str(data.get("title") or record_id.split(":")[-1])
            command = str(data.get("agent_command") or "")
            rows.append({
                "recordId": record_id,
                "sessionId": str(data.get("acp_session_id") or ""),
                "title": title[:160],
                "command": command[:180],
                "cwd": str(data.get("cwd") or "")[:220],
                "pid": pid or None,
                "pidAlive": pid_alive,
                "closed": closed,
                "state": "running" if pid_alive and not closed else ("closed" if closed else "stale"),
                "createdAt": created_ms,
                "updatedAt": updated_ms or created_ms,
                "messageCount": len(messages),
                "path": str(path),
            })
    rows.sort(key=lambda item: int(item.get("updatedAt") or 0), reverse=True)
    return {
        "total": len(rows),
        "active": sum(1 for r in rows if r.get("state") == "running"),
        "stale": sum(1 for r in rows if r.get("state") == "stale"),
        "closed": sum(1 for r in rows if r.get("state") == "closed"),
        "sessions": rows[:limit],
    }


def cleanup_stale_acp_sessions() -> dict[str, Any]:
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    closed: list[str] = []
    for directory in ACP_SESSION_DIRS:
        if not directory.exists():
            continue
        for path in directory.glob("*acp*.json"):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            if data.get("schema") != "acpx.session.v1" and "acp_session_id" not in data:
                continue
            raw_pid = data.get("pid")
            pid = int(raw_pid) if str(raw_pid or "").isdigit() else 0
            pid_alive = bool(pid and Path(f"/proc/{pid}").exists())
            already_closed = bool(data.get("closed") or data.get("closed_at"))
            if pid_alive or already_closed:
                continue
            data["closed"] = True
            data["closed_at"] = now_iso
            data.setdefault("acpx", {})
            if isinstance(data.get("acpx"), dict):
                data["acpx"]["closedByObserverSite"] = True
                data["acpx"]["closedReason"] = "stale pid missing"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            closed.append(str(path))
    return {"ok": True, "closed": len(closed), "paths": closed[:20], "acpSessions": summarize_acp_sessions()}


def spawn_acp_task(data: dict[str, Any]) -> dict[str, Any]:
    task = str(data.get("task") or "").strip()
    if not task:
        raise ValueError("ACP 작업 내용을 입력하세요.")
    target_agent = str(data.get("acpAgentId") or data.get("targetAgent") or "codex").strip() or "codex"
    cwd = str(data.get("cwd") or "/mnt/c/MegaGrit/RogueLike_001").strip()
    label = str(data.get("label") or "observer-acp-task").strip()[:80] or "observer-acp-task"
    mode = str(data.get("mode") or "session").strip()
    params = {
        "task": task,
        "runtime": "acp",
        "agentId": target_agent,
        "cwd": cwd,
        "mode": mode if mode in {"run", "session"} else "session",
        "thread": False,
        "label": label,
    }
    result = gateway_call("sessions.spawn", params, timeout=30)
    return {"ok": True, "params": params, "result": result, "acpSessions": summarize_acp_sessions()}


def summarize_agents() -> dict[str, Any]:
    now = int(time.time() * 1000)
    sanitize_internal_report_contract_messages()
    sanitize_internal_done_messages()
    expire_stale_pending_assignments(now)
    sessions = get_sessions_cached(timeout=18)

    output = []
    for raw_base in AGENTS:
        base = apply_agent_settings(raw_base)
        agent_sessions = [s for s in sessions if isinstance(s, dict) and (s.get("agentId") == base["id"] or str(s.get("key", "")).startswith(f"agent:{base['id']}:") )]
        agent_sessions.sort(key=lambda s: to_epoch_ms(s.get("updatedAt") or s.get("startedAt")), reverse=True)
        state_sessions = [s for s in agent_sessions if not is_nonblocking_maintenance_session(s)]
        active = [s for s in state_sessions if normalize_status(str(s.get("status") or "")) == "working"]
        recent_active = []
        stale_active = []
        for session in active:
            updated = to_epoch_ms(session.get("updatedAt") or session.get("startedAt"))
            session_stale_ms = max(0, now - updated) if updated else ACTIVE_SESSION_MAX_STALE_MS + 1
            if session_stale_ms <= ACTIVE_SESSION_MAX_STALE_MS:
                recent_active.append(session)
            else:
                stale_active.append(session)
        latest = state_sessions[0] if state_sessions else (agent_sessions[0] if agent_sessions else None)
        current = recent_active[0] if recent_active else latest
        chat_rows = agent_session_key_rows(base["id"], agent_sessions)
        chat_key = observer_chat_session_key_from_rows(base["id"], chat_rows)
        chat_session = next((s for s in agent_sessions if str(s.get("key") or "") == chat_key), None)
        context_source = chat_session or current

        report_pending_ms = latest_report_pending_ms(base["id"])
        pending_assignment_ms = latest_pending_assignment_ms(base["id"], now) or latest_unanswered_user_ms(base["id"])
        state = "working" if recent_active else ("reporting" if report_pending_ms else ("assigned" if pending_assignment_ms else "idle"))
        if report_pending_ms:
            pending_assignment_ms = report_pending_ms
        current_started = to_epoch_ms(current.get("startedAt") or current.get("updatedAt")) if current else pending_assignment_ms
        current_updated = to_epoch_ms(current.get("updatedAt") or current.get("startedAt")) if current else 0
        age_ms = max(0, now - current_started) if current_started else 0
        stale_ms = max(0, now - current_updated) if current_updated else 0
        raw_status = str(current.get("status") or "unknown") if current else "none"

        # 현재 작업중 여부는 최근 갱신된 running/queued/processing 세션만 인정한다.
        # 오래 갱신되지 않은 running 세션은 중단/종료 뒤 남은 고아 상태일 수 있으므로
        # 지연 가능/점검으로 올리지 않고 제외한다.
        is_lagging = False
        if not active and normalize_status(raw_status) == "warning" and stale_ms < 120_000:
            state = "warning"

        latest_updated = current_updated
        context_tokens = int(float(context_source.get("totalTokens") or 0) or 0) if context_source else 0
        context_max_tokens = int(float(context_source.get("contextTokens") or DEFAULT_CONTEXT_MAX_TOKENS) or DEFAULT_CONTEXT_MAX_TOKENS) if context_source else DEFAULT_CONTEXT_MAX_TOKENS
        current_model = str(context_source.get("model") or "") if context_source else ""
        current_provider = str(context_source.get("modelProvider") or "") if context_source else ""
        if current_model.startswith("gpt-5") or current_provider == "openai-codex":
            context_max_tokens = max(context_max_tokens, DEFAULT_CONTEXT_MAX_TOKENS)
        context_remaining_percent = max(0, min(100, round((1 - (context_tokens / context_max_tokens)) * 100))) if context_max_tokens else 0
        status_text = "응답 가능"
        if state == "working":
            status_text = "작업 중"
        elif state == "assigned":
            status_text = "응답 대기"
        elif state == "reporting":
            status_text = "보고 대기"
        elif state == "warning":
            status_text = "점검 필요"

        detail = "현재 생성 중 아님 · 최근 실행 세션 없음"
        if state == "assigned":
            pending_age_s = max(0, (now - pending_assignment_ms) // 1000)
            status_text = "응답 지연" if pending_age_s * 1000 > ASSIGNED_MAX_AGE_MS else "응답 대기"
            detail = f"{status_text} · 아직 최종 답변 없음 · 요청 {pending_age_s:,}초 전"
        if state == "reporting":
            pending_age_s = max(0, (now - pending_assignment_ms) // 1000)
            detail = f"보고 대기 · 시작/진행 보고 이후 최종 완료보고 없음 · 요청 {pending_age_s:,}초 전"
        if current and state not in {"assigned", "reporting"}:
            total_tokens = context_tokens
            model = current.get("model") or "unknown"
            if recent_active:
                detail = f"현재 작업 중: {raw_status} · 마지막 갱신 {stale_ms // 1000:,}초 전 · {model} · {total_tokens:,} tokens"
            else:
                stale_note = f" · 고아 running 세션 {len(stale_active)}개 제외" if stale_active else ""
                recent_note = f" · 최근 활동 {stale_ms // 1000:,}초 전" if current_updated else ""
                detail = f"현재 생성 중 아님{stale_note}{recent_note} · 최근 세션: {raw_status} · {model} · {total_tokens:,} tokens"

        subagent_summary = summarize_subagents(base["id"], sessions, now)
        report_debt = latest_report_debt(base["id"], agent_sessions, chat_key)
        if report_debt and state == "idle":
            state = "reporting"
            status_text = "보고 누락"
            debt_age_s = max(0, (now - int(report_debt.get("ts") or now)) // 1000)
            detail = f"보고 누락 · {report_debt.get('reason') or '작업 증거'} · {debt_age_s:,}초 전"
        active_work = active_work_item(base["id"])
        self_work = active_self_work_item() if base["id"] == "observer" else None
        self_work_age_ms = 0
        self_work_report_due = False
        if self_work:
            started = int(self_work.get("startedAt") or now)
            last_report = int(self_work.get("lastReportAt") or started)
            self_work_age_ms = max(0, now - started)
            self_work_report_due = now - last_report >= SELF_WORK_REPORT_INTERVAL_MS
            if state == "idle":
                state = "reporting"
                status_text = "보고 대기"
                detail = f"보고 대기 · 후타바 직접 작업 최종 보고 없음 · 요청 {self_work_age_ms // 1000:,}초 전"
            elif self_work_report_due:
                status_text = f"{status_text} · 보고 기한 초과"
                detail = f"보고 기한 초과 · 후타바 직접 작업 중간/완료 보고 없음 · 요청 {self_work_age_ms // 1000:,}초 전"
        if active_work and state == "idle":
            state = "reporting"
            status_text = "보고 대기"
            started = int(active_work.get("startedAt") or now)
            detail = f"보고 대기 · 등록 작업 최종 보고 없음 · 요청 {(now - started) // 1000:,}초 전"
        last_visible_report_ts = latest_visible_agent_report_ts(base["id"], agent_sessions, chat_key)
        work_started_ts = int(active_work.get("startedAt") or 0) if isinstance(active_work, dict) else 0
        silence_base_ts = max(last_visible_report_ts, work_started_ts)
        silence_age_ms = max(0, now - silence_base_ts) if silence_base_ts else 0
        needs_silence_report = state in {"working", "assigned", "reporting"} and silence_age_ms >= SILENCE_WARNING_MS
        if needs_silence_report:
            status_text = "무음 경고" if state == "working" else f"{status_text} · 무음"
            silence_note = f"마지막 가시 보고 {silence_age_ms // 1000:,}초 전"
            detail = f"무음 경고 · {silence_note} · {detail}"
        agent_payload = {
            **base,
            "state": state,
            "statusText": status_text,
            "detail": detail,
            "sessionCount": len(agent_sessions),
            "activeSessionCount": len(recent_active),
            "staleActiveSessionCount": len(stale_active),
            "isWorkingNow": bool(recent_active),
            "isAssigned": state == "assigned",
            "isReportPending": state == "reporting",
            "reportDebt": report_debt,
            "lastVisibleReportTs": last_visible_report_ts,
            "silenceAgeMs": silence_age_ms,
            "needsSilenceReport": needs_silence_report,
            "activeWork": active_work,
            "selfWork": self_work if base["id"] == "observer" else None,
            "selfWorkAgeMs": self_work_age_ms if base["id"] == "observer" else 0,
            "selfWorkReportDue": self_work_report_due if base["id"] == "observer" else False,
            "controlRoute": "observer-web",
            "chatSessionKey": chat_key,
            "isLagging": is_lagging,
            "ageSeconds": age_ms // 1000 if current else 0,
            "staleSeconds": stale_ms // 1000 if current else 0,
            "latestUpdatedAt": latest_updated,
            "latestSessionKey": current.get("key") if current else None,
            "latestPreview": current.get("lastMessagePreview") if current else None,
            "contextTokens": context_tokens,
            "contextMaxTokens": context_max_tokens,
            "contextSessionKey": context_source.get("key") if context_source else None,
            "contextRemainingPercent": context_remaining_percent,
            "subagents": subagent_summary,
            "unreadCount": unread_count(base["id"]),
        }
        output.append(agent_payload)
        maybe_auto_nudge(agent_payload)

    ops_alerts = summarize_ops_alerts()
    return {
        "ok": True,
        "generatedAt": int(time.time() * 1000),
        "agents": output,
        "opsAlerts": ops_alerts,
        "acpSessions": summarize_acp_sessions(),
        "counts": {
            "total": len(output),
            "idle": sum(1 for a in output if a["state"] == "idle"),
            "working": sum(1 for a in output if a["state"] == "working"),
            "assigned": sum(1 for a in output if a["state"] == "assigned"),
            "reporting": sum(1 for a in output if a["state"] == "reporting"),
            "reportMissing": sum(1 for a in output if a.get("reportDebt")),
            "silenceWarning": sum(1 for a in output if a.get("needsSilenceReport")),
            "warning": sum(1 for a in output if a["state"] == "warning"),
        },
    }


def html_escape(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def state_label(state: str, agent: dict[str, Any] | None = None) -> str:
    if state == "working":
        return "작업중"
    if state == "assigned":
        return "업무 할당됨"
    if state == "warning":
        return "점검"
    return "대기"


def state_badge_class(state: str, agent: dict[str, Any] | None = None) -> str:
    if state == "working":
        return "work"
    if state == "assigned":
        return "assigned"
    if state == "warning":
        return "warn"
    return "idle"


def render_agent_card(agent: dict[str, Any]) -> str:
    state = str(agent.get("state") or "idle")
    name = html_escape(agent.get("name") or agent.get("id") or "agent")
    orb = html_escape(agent.get("orb") or name[:1])
    avatar = agent.get("avatar") if isinstance(agent.get("avatar"), str) else None
    orb_html = f'<img src="{html_escape(avatar)}" alt="" />' if avatar else orb
    detail = html_escape(agent.get("detail") or agent.get("statusText") or "상태 확인 중")
    working_tag = f"실행중 {agent.get('activeSessionCount') or 1}" if agent.get("isWorkingNow") else ("업무 할당됨" if agent.get("isAssigned") else "현재 생성 중 아님")
    tags = [agent.get("role"), working_tag, *(agent.get("tags") or []), agent.get("id")]
    tag_html = "".join(f'<span class="tag">{html_escape(tag)}</span>' for tag in tags[:6] if tag)
    subagents = agent.get("subagents") or {}
    dots = "".join(f'<span class="sub-dot {html_escape(item.get("state"))}" title="{html_escape(item.get("status"))} · {html_escape(item.get("staleSeconds"))}초 전"></span>' for item in (subagents.get("recent") or [])[:12])
    sub_html = ""
    if subagents.get("total"):
        hidden = subagents.get("hiddenTotal") or 0
        hidden_text = f" · 숨김 {html_escape(hidden)}" if hidden else ""
        sub_html = f'<div class="subagent-strip"><div class="subagent-dots">{dots}</div><div class="subagent-text">실행 중 하위 작업 {html_escape(subagents.get("total"))} · 정상 {html_escape(subagents.get("working"))}{hidden_text}</div></div>'
    badge = state_label(state, agent)
    badge_class = state_badge_class(state, agent)
    work_class = "" if state == "idle" else "work"
    lag_class = ""
    warn_class = "warn" if state == "warning" else ""
    accent = "idle" if state == "idle" else "work"
    return f"""
            <article class="agent {work_class} {lag_class} {warn_class}" role="button" tabindex="0" data-agent="{name}" data-state="{html_escape(agent.get('statusText') or '상태 확인 중')}" data-accent="{accent}" data-agent-id="{html_escape(agent.get('id'))}">
              <span class="state-badge {badge_class}">{badge}</span>
              <div class="orb">{orb_html}</div>
              <div>
                <div class="name">{name} <span class="pill">{badge}</span></div>
                <div class="status">{detail}</div>
                <div class="tag-row">{tag_html}</div>
                {sub_html}
              </div>
            </article>"""


def replace_between(source: str, start: str, end: str, replacement: str) -> str:
    start_index = source.index(start) + len(start)
    end_index = source.index(end, start_index)
    return source[:start_index] + replacement + source[end_index:]


def render_initial_page() -> bytes:
    html = (ROOT / "index.html").read_text()
    try:
        payload = summarize_agents()
        agents = payload.get("agents") if isinstance(payload, dict) else []
        counts = payload.get("counts") if isinstance(payload, dict) else {}
        idle = [a for a in agents if a.get("state") == "idle"]
        active = [a for a in agents if a.get("state") != "idle"]
        idle_html = "\n".join(render_agent_card(a) for a in idle) or '<article class="agent"><div class="orb">✓</div><div><div class="name">대기 없음</div><div class="status">모든 에이전트가 작업 중이거나 확인 필요 상태입니다.</div></div></article>'
        active_html = "\n".join(render_agent_card(a) for a in active)
        html = replace_between(
            html,
            '<div class="agent-grid" id="idleAgents">',
            '</div>\n        </section>\n\n        <section class="lane working">',
            "\n" + idle_html + "\n          ",
        )
        html = replace_between(
            html,
            '<div class="agent-grid" id="workingAgents">',
            '</div>\n        </section>\n      </section>',
            "\n" + active_html + "\n          ",
        )
        total = counts.get("total", len(agents))
        idle_count = counts.get("idle", len(idle))
        active_count = (counts.get("working") or 0) + (counts.get("warning") or 0)
        html = html.replace('<strong id="countTotal">-</strong>', f'<strong id="countTotal">{html_escape(total)}</strong>')
        html = html.replace('<strong id="countIdle">-</strong>', f'<strong id="countIdle">{html_escape(idle_count)}</strong>')
        html = html.replace('<strong id="countWorking">-</strong>', f'<strong id="countWorking">{html_escape(active_count)}</strong>')
        html = html.replace('<div class="count" id="idleLabel">대기 에이전트</div>', f'<div class="count" id="idleLabel">{html_escape(idle_count)} 대기</div>')
        html = html.replace('<div class="count" id="workingLabel">작업 에이전트</div>', f'<div class="count" id="workingLabel">{html_escape(active_count)} 작업/점검</div>')
    except Exception:
        pass
    return html.encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def json_response(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            body = render_initial_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/uploads/"):
            from urllib.parse import unquote
            name = Path(unquote(path.rsplit("/", 1)[-1])).name
            file_path = UPLOAD_DIR / name
            if not file_path.is_file():
                self.send_error(404, "upload not found")
                return
            mime_by_ext = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
            mime = mime_by_ext.get(file_path.suffix.lower(), "application/octet-stream")
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/agent/settings":
            self.json_response({"ok": True, "settings": load_agent_settings()})
            return
        if path == "/api/chat/history":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            agent_id = (qs.get("agentId") or ["observer"])[0]
            if agent_id not in {a["id"] for a in AGENTS}:
                self.json_response({"ok": False, "error": "허용되지 않은 에이전트입니다."}, 400)
                return
            mark_read = (qs.get("markRead") or ["0"])[0] in {"1", "true", "yes"}
            self.json_response(public_history(agent_id, mark_read=mark_read))
            return
        if path == "/api/agents":
            cached = cache_get("agents")
            if cache_is_fresh(cached):
                self.json_response(cached)
                return
            try:
                payload = cache_set("agents", summarize_agents())
            except Exception as exc:  # keep UI alive with last known good state
                payload = stale_payload("agents", exc)
                if not payload:
                    payload = {"ok": False, "error": str(exc), "generatedAt": int(time.time() * 1000), "agents": [], "counts": {"total": 0, "idle": 0, "working": 0, "warning": 0}}
                    self.json_response(payload, 502)
                    return
            self.json_response(payload)
            return
        if path == "/api/observer/self-work":
            self.json_response({"ok": True, "state": load_self_work_state(), "active": active_self_work_item()})
            return
        if path == "/api/gateway-status":
            cached = cache_get("gateway")
            if cache_is_fresh(cached):
                self.json_response(cached)
                return
            try:
                payload = cache_set("gateway", summarize_gateway_status())
            except Exception as exc:
                payload = stale_payload("gateway", exc)
                if not payload:
                    payload = {"ok": False, "state": "warning", "error": str(exc), "generatedAt": int(time.time() * 1000)}
                    self.json_response(payload, 503)
                    return
            self.json_response(payload, 200 if payload.get("ok") else 503)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        try:
            data = read_body_json(self)
            agent_id = str(data.get("agentId") or "observer")
            if path == "/api/chat/send":
                message = str(data.get("message") or "")
                result = send_chat(agent_id, message, data.get("attachments") if isinstance(data.get("attachments"), list) else None)
                self.json_response(result, 200 if result.get("ok") else 504)
                return
            if path == "/api/agent/recover":
                result = send_recovery_nudge(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/force-check":
                result = send_force_check(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/session-detail":
                result = get_agent_session_detail(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/abort":
                result = abort_agent_session(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/cleanup-orphans":
                result = abort_orphan_subagent_sessions(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/archive-candidates":
                result = archive_subagent_candidate_sessions(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/new-chat-session":
                result = create_new_observer_chat_session(agent_id, "manual")
                self.json_response(result)
                return
            if path == "/api/agent/settings":
                result = save_single_agent_setting(agent_id, data.get("displayName"), data.get("avatar"))
                self.json_response(result)
                return
            if path == "/api/acp/cleanup-stale":
                result = cleanup_stale_acp_sessions()
                self.json_response(result)
                return
            if path == "/api/acp/spawn":
                result = spawn_acp_task(data)
                self.json_response(result)
                return
            if path == "/api/observer/self-work/register":
                result = register_self_work(str(data.get("message") or ""), str(data.get("requestId") or "") or None)
                self.json_response({"ok": True, "item": result})
                return
            if path == "/api/observer/self-work/report":
                result = update_self_work(str(data.get("requestId") or ""), str(data.get("status") or "reported"), str(data.get("report") or ""), append_chat_report=True)
                self.json_response({"ok": bool(result), "item": result})
                return
            self.json_response({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8788"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"observer-site serving on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
