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
CACHE_LOCK = threading.Lock()

NUDGE_PATH = DATA_DIR / "recovery-nudges.json"
NUDGE_INTERVAL_SECONDS = 900
# 세션 목록의 running 상태는 비정상 종료/중단 뒤에도 남을 수 있다.
# 관제 화면은 "현재 작업"만 보여야 하므로, 최근 갱신이 없는 running 세션은
# 작업/지연으로 올리지 않고 고아 세션으로 제외한다.
ACTIVE_SESSION_MAX_STALE_MS = 3 * 60 * 1000
SUBAGENT_ACTIVE_MAX_STALE_MS = 3 * 60 * 1000
ASSIGNED_MAX_AGE_MS = 90 * 1000


def cache_get(name: str) -> dict[str, Any] | None:
    with CACHE_LOCK:
        cached = AGENTS_CACHE if name == "agents" else GATEWAY_CACHE
        if not isinstance(cached, dict):
            return None
        age_ms = int(time.time() * 1000) - int(cached.get("generatedAt") or 0)
        return {**cached, "cacheAgeMs": max(0, age_ms)}


def cache_set(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    global AGENTS_CACHE, GATEWAY_CACHE
    with CACHE_LOCK:
        if name == "agents":
            AGENTS_CACHE = payload
        else:
            GATEWAY_CACHE = payload
    return payload


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
    state[agent_id] = int(time.time() * 1000)
    save_read_state(state)


def unread_count(agent_id: str) -> int:
    last_read = load_read_state().get(agent_id, 0)
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


def append_history(agent_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    history = load_history()
    item = {"role": role, "content": content, "ts": int(time.time() * 1000), **(meta or {})}
    entries = history.setdefault(agent_id, [])
    entries.append(item)
    del entries[:-80]
    save_history(history)
    return item


def replace_history_message(agent_id: str, request_id: str, role: str, content: str, meta: dict[str, Any] | None = None) -> dict[str, Any] | None:
    history = load_history()
    entries = history.setdefault(agent_id, [])
    for index, item in enumerate(entries):
        if item.get("requestId") == request_id and item.get("role") == role:
            updated = {**item, "content": content, "ts": int(time.time() * 1000), **(meta or {})}
            entries[index] = updated
            save_history(history)
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



def desired_observer_chat_session_key(agent_id: str) -> str:
    return f"agent:{agent_id}:observer-site-chat"


def existing_agent_session_keys(agent_id: str) -> list[dict[str, Any]]:
    try:
        payload = gateway_call("sessions.list", {"limit": 120}, timeout=12)
    except Exception:
        return []
    sessions = payload.get("sessions") if isinstance(payload, dict) else []
    if not isinstance(sessions, list):
        return []
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


def observer_chat_session_key(agent_id: str) -> str:
    """Return an existing sendable session key for the agent.

    OpenClaw sessions.send does not create arbitrary keys. A synthetic key like
    agent:observer:observer-site-chat therefore fails with "session not found"
    unless such a session already exists. Prefer the dedicated observer-site key
    when present, otherwise fall back to an existing direct session for the same
    agent.
    """
    desired = desired_observer_chat_session_key(agent_id)
    rows = existing_agent_session_keys(agent_id)
    keys = [str(row.get("key") or "") for row in rows]
    if desired in keys:
        return desired
    preferred_suffixes = (":observer-site", ":telegram:direct:7872172509", f":{agent_id}")
    for suffix in preferred_suffixes:
        for row in rows:
            key = str(row.get("key") or "")
            status = str(row.get("status") or "").lower()
            if key.endswith(suffix) and status not in {"failed", "error"}:
                return key
    for row in rows:
        status = str(row.get("status") or "").lower()
        key = str(row.get("key") or "")
        if status not in {"failed", "error"} and key:
            return key
    for suffix in preferred_suffixes:
        for key in keys:
            if key.endswith(suffix):
                return key
    return desired

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


def public_history(agent_id: str, mark_read: bool = False) -> dict[str, Any]:
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
        if item.get("role") == "assistant" and status == "synced":
            return False
        content = str(item.get("content") or "")
        if status == "pending":
            return True
        if "응답 연결이 잠시 끊겨" in content or "응답 대기가 만료" in content:
            return False
        if content.startswith("백그라운드 실행을 시작"):
            return False
        return not is_internal_status_text(content)

    visible = [item for item in messages if is_visible(item)]
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



def is_internal_status_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith('{'):
        try:
            payload = json.loads(stripped)
            if is_transport_status_payload(payload):
                return True
            if isinstance(payload, dict) and payload.get("role") == "assistant":
                content = payload.get("content")
                # Reasoning/tool-only assistant payloads are internal artifacts,
                # not user-visible completion reports.
                if isinstance(content, list) and not extract_content_text(content):
                    return True
        except Exception:
            pass
    bad_prefixes = (
        "백그라운드 실행을 시작",
        "메시지는 일반 세션",
        "응답 대기가 만료",
        "응답 도착",
        "전달됨.",
    )
    return stripped.startswith(bad_prefixes)


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
        if not text or is_internal_status_text(text):
            text = "실패: 내부 실행 상태만 받았고 최종 답변을 아직 찾지 못했습니다. 잠시 후 다시 확인해 주세요."
        replace_history_message(
            agent_id,
            request_id,
            "system",
            text,
            {"status": "done", "done": True, "pending": False, "sessionKey": session_key, "role": "assistant"},
        )
    except Exception as exc:
        replace_history_message(
            agent_id,
            request_id,
            "system",
            f"실패: 에이전트 최종 응답을 받지 못했습니다. {exc}",
            {"status": "error", "error": True, "done": True, "pending": False, "sessionKey": session_key},
        )


def send_chat(agent_id: str, message: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    validate_chat_message(agent_id, message, attachments)
    agent = next(a for a in AGENTS if a["id"] == agent_id)
    session_key = observer_chat_session_key(agent_id)
    request_id = str(uuid.uuid4())
    saved_attachments = save_chat_attachments(agent_id, request_id, attachments)
    outbound_message = build_message_with_attachments(message, saved_attachments)
    display_message = message.strip() or "이미지 첨부"
    if saved_attachments:
        display_message += "\n" + "\n".join(f"[이미지] {item['path']}" for item in saved_attachments)
    append_history(agent_id, "user", display_message, {"requestId": request_id, "sessionKey": session_key, "attachments": saved_attachments})
    append_history(agent_id, "system", "전달됨. 응답을 기다리는 중입니다...", {"requestId": request_id, "sessionKey": session_key, "status": "pending", "pending": True})
    worker = threading.Thread(target=complete_chat_async, args=(agent_id, agent["name"], session_key, outbound_message, request_id), daemon=True)
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


def maybe_auto_nudge(agent: dict[str, Any]) -> None:
    agent_id = str(agent.get("id") or "")
    if not agent_id or not agent.get("isLagging"):
        return
    now = int(time.time())
    nudges = load_nudges()
    last = int(nudges.get(agent_id, 0) or 0)
    if now - last < NUDGE_INTERVAL_SECONDS:
        return
    nudges[agent_id] = now
    save_nudges(nudges)
    threading.Thread(target=lambda: send_recovery_nudge(agent_id), daemon=True).start()


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
            compact_messages.append({"role": role, "content": content[:2000], "ts": msg.get("ts") or msg.get("timestamp") or msg.get("createdAt")})
    else:
        for msg in public_history(agent_id, mark_read=False).get("messages", [])[-20:]:
            if not isinstance(msg, dict):
                continue
            compact_messages.append({
                "role": msg.get("role") or "message",
                "content": str(msg.get("content") or "")[:2000],
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
        item = {
            "key": key,
            "state": state,
            "status": status,
            "staleSeconds": stale_seconds,
            "model": session.get("model"),
            "tokens": int(float(session.get("totalTokens") or 0) or 0),
            "preview": preview[:500],
        }
        subs.append(item)
    subs.sort(key=lambda item: item.get("staleSeconds", 0))
    visible = [item for item in subs if item["state"] == "working"]
    recent = visible[:12]
    return {
        "total": len(visible),
        "hiddenTotal": max(0, len(subs) - len(visible)),
        "recent": recent,
        "done": 0,
        "working": sum(1 for item in visible if item["state"] == "working"),
        "lag": 0,
        "failed": 0,
    }



def terminal_reply_ts(entries: list[dict[str, Any]]) -> int:
    latest = 0
    for item in entries:
        if item.get("role") != "assistant":
            continue
        if item.get("status") not in {"done", "error"}:
            continue
        if is_internal_status_text(str(item.get("content") or "")):
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


def sanitize_internal_done_messages() -> int:
    history = load_history()
    changed = 0
    for entries in history.values():
        for item in entries:
            if item.get("role") == "assistant" and item.get("status") == "done" and is_internal_status_text(str(item.get("content") or "")):
                item["status"] = "internal-status"
                item["hidden"] = True
                item["pending"] = False
                changed += 1
    if changed:
        save_history(history)
    return changed

def summarize_agents() -> dict[str, Any]:
    now = int(time.time() * 1000)
    sanitize_internal_done_messages()
    expire_stale_pending_assignments(now)
    sessions_data = gateway_call("sessions.list", timeout=18)
    sessions = sessions_data.get("sessions") if isinstance(sessions_data, dict) else []
    if not isinstance(sessions, list):
        sessions = []

    output = []
    for raw_base in AGENTS:
        base = apply_agent_settings(raw_base)
        agent_sessions = [s for s in sessions if isinstance(s, dict) and (s.get("agentId") == base["id"] or str(s.get("key", "")).startswith(f"agent:{base['id']}:") )]
        agent_sessions.sort(key=lambda s: to_epoch_ms(s.get("updatedAt") or s.get("startedAt")), reverse=True)
        active = [s for s in agent_sessions if normalize_status(str(s.get("status") or "")) == "working"]
        recent_active = []
        stale_active = []
        for session in active:
            updated = to_epoch_ms(session.get("updatedAt") or session.get("startedAt"))
            session_stale_ms = max(0, now - updated) if updated else ACTIVE_SESSION_MAX_STALE_MS + 1
            if session_stale_ms <= ACTIVE_SESSION_MAX_STALE_MS:
                recent_active.append(session)
            else:
                stale_active.append(session)
        latest = agent_sessions[0] if agent_sessions else None
        current = recent_active[0] if recent_active else latest

        pending_assignment_ms = latest_pending_assignment_ms(base["id"], now) or latest_unanswered_user_ms(base["id"])
        state = "working" if recent_active else ("assigned" if pending_assignment_ms else "idle")
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
        status_text = "응답 가능"
        if state == "working":
            status_text = "작업 중"
        elif state == "assigned":
            status_text = "응답 대기"
        elif state == "warning":
            status_text = "점검 필요"

        detail = "현재 생성 중 아님 · 최근 실행 세션 없음"
        if state == "assigned":
            pending_age_s = max(0, (now - pending_assignment_ms) // 1000)
            status_text = "응답 지연" if pending_age_s * 1000 > ASSIGNED_MAX_AGE_MS else "응답 대기"
            detail = f"{status_text} · 아직 최종 답변 없음 · 요청 {pending_age_s:,}초 전"
        if current and state != "assigned":
            total_tokens = int(float(current.get("totalTokens") or 0) or 0)
            model = current.get("model") or "unknown"
            if recent_active:
                detail = f"현재 작업 중: {raw_status} · 마지막 갱신 {stale_ms // 1000:,}초 전 · {model} · {total_tokens:,} tokens"
            else:
                stale_note = f" · 고아 running 세션 {len(stale_active)}개 제외" if stale_active else ""
                recent_note = f" · 최근 활동 {stale_ms // 1000:,}초 전" if current_updated else ""
                detail = f"현재 생성 중 아님{stale_note}{recent_note} · 최근 세션: {raw_status} · {model} · {total_tokens:,} tokens"

        subagent_summary = summarize_subagents(base["id"], sessions, now)
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
            "isLagging": is_lagging,
            "ageSeconds": age_ms // 1000 if current else 0,
            "staleSeconds": stale_ms // 1000 if current else 0,
            "latestUpdatedAt": latest_updated,
            "latestSessionKey": current.get("key") if current else None,
            "latestPreview": current.get("lastMessagePreview") if current else None,
            "subagents": subagent_summary,
            "unreadCount": unread_count(base["id"]),
        }
        output.append(agent_payload)
        maybe_auto_nudge(agent_payload)

    return {
        "ok": True,
        "generatedAt": int(time.time() * 1000),
        "agents": output,
        "counts": {
            "total": len(output),
            "idle": sum(1 for a in output if a["state"] == "idle"),
            "working": sum(1 for a in output if a["state"] == "working"),
            "assigned": sum(1 for a in output if a["state"] == "assigned"),
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
            if path == "/api/agent/session-detail":
                result = get_agent_session_detail(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/abort":
                result = abort_agent_session(agent_id)
                self.json_response(result)
                return
            if path == "/api/agent/settings":
                result = save_single_agent_setting(agent_id, data.get("displayName"), data.get("avatar"))
                self.json_response(result)
                return
            self.json_response({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8788"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"observer-site serving on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
