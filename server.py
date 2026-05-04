#!/usr/bin/env python3
"""Observer site backend.

Serves the static homepage, live /api/agents monitoring, and a guarded
agent-chat bridge for the three allowed OpenClaw agents.
"""

from __future__ import annotations

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
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "/home/oopogo/.npm-global/bin/openclaw")
AGENTS = [
    {"id": "main", "name": "밀레느", "orb": "밀", "role": "메인 작업 에이전트", "tags": ["MAIN", "ORCHESTRATOR", "게임"]},
    {"id": "observer", "name": "observer", "orb": "옵", "role": "운영 감시자", "tags": ["OPS", "GATEWAY", "감시"]},
    {"id": "mediacontentproducer", "name": "미디어", "orb": "미", "role": "콘텐츠 프로듀서", "tags": ["MEDIA", "CRON-PAUSED", "콘텐츠"]},
]
CACHE_TTL_SECONDS = 8
AGENTS_CACHE: dict[str, Any] | None = None
GATEWAY_CACHE: dict[str, Any] | None = None
CACHE_LOCK = threading.Lock()

NUDGE_PATH = DATA_DIR / "recovery-nudges.json"
NUDGE_INTERVAL_SECONDS = 900
ACTIVE_SESSION_MAX_STALE_MS = 30 * 60 * 1000


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
    if length <= 0 or length > 64_000:
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


def public_history(agent_id: str) -> dict[str, Any]:
    return {"ok": True, "agentId": agent_id, "messages": load_history().get(agent_id, [])[-80:]}


def extract_response_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    for key in ("output_text", "text", "message", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
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


def validate_chat_message(agent_id: str, message: str) -> None:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    if not message.strip():
        raise ValueError("메시지가 비어 있습니다.")
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


def send_via_sessions_fallback(agent_id: str, message: str, timeout: int = 90) -> str:
    """Fallback path when OpenResponses closes before returning text."""
    session_key = f"agent:{agent_id}:observer-site"
    args = [
        OPENCLAW_BIN,
        "gateway",
        "call",
        "sessions.send",
        "--expect-final",
        "--json",
        "--timeout",
        str(timeout * 1000),
        "--params",
        json.dumps({"key": session_key, "message": message}, ensure_ascii=False),
    ]
    env = os.environ.copy()
    env.pop("OPENCLAW_GATEWAY_URL", None)
    env.pop("OPENCLAW_API_URL", None)
    env["NO_COLOR"] = "1"
    result = subprocess.run(args, cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout + 8)
    raw = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(raw or "fallback send failed")
    payload = parse_json_suffix(raw)
    text = extract_response_text(payload)
    if text and text not in {"{}", "[]"}:
        return text
    status = payload.get("status") if isinstance(payload, dict) else None
    run_id = payload.get("runId") if isinstance(payload, dict) else None
    if status or run_id:
        return f"메시지는 일반 세션 경로로 다시 전달했습니다. 상태: {status or '전달됨'}"
    return "메시지는 일반 세션 경로로 다시 전달했습니다."


def complete_chat_async(agent_id: str, agent_name: str, session_key: str, message: str, request_id: str) -> None:
    token = read_gateway_token()
    body = json.dumps({
        "model": f"openclaw:{agent_id}",
        "input": message,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-openclaw-agent-id": agent_id,
        "x-openclaw-session-key": session_key,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request("http://127.0.0.1:18789/v1/responses", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as res:
            raw = res.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
            text = extract_response_text(payload)
            replace_history_message(agent_id, request_id, "system", "응답 도착", {"status": "done", "done": True, "sessionKey": session_key})
            append_history(agent_id, "assistant", text, {"sessionKey": session_key, "requestId": request_id, "status": "done"})
    except Exception as exc:
        try:
            fallback_text = send_via_sessions_fallback(agent_id, message)
            replace_history_message(
                agent_id,
                request_id,
                "system",
                "응답 연결이 잠시 끊겨 일반 세션 경로로 다시 전달했습니다.",
                {"status": "fallback", "done": True, "sessionKey": session_key},
            )
            if fallback_text:
                append_history(agent_id, "assistant", fallback_text, {"sessionKey": session_key, "requestId": request_id, "status": "fallback"})
        except Exception as fallback_exc:
            text = "응답 연결이 중간에 끊겼습니다. 메시지는 기록했지만, 에이전트 응답을 받지 못했습니다. 다시 보내주세요."
            replace_history_message(
                agent_id,
                request_id,
                "system",
                text,
                {"status": "error", "error": True, "done": True, "sessionKey": session_key, "detail": str(fallback_exc), "originalError": str(exc)},
            )


def send_chat(agent_id: str, message: str) -> dict[str, Any]:
    validate_chat_message(agent_id, message)
    agent = next(a for a in AGENTS if a["id"] == agent_id)
    session_key = f"agent:{agent_id}:observer-site"
    request_id = str(uuid.uuid4())
    append_history(agent_id, "user", message, {"requestId": request_id, "sessionKey": session_key})
    append_history(agent_id, "system", "전달됨. 응답을 기다리는 중입니다...", {"requestId": request_id, "sessionKey": session_key, "status": "pending", "pending": True})
    worker = threading.Thread(target=complete_chat_async, args=(agent_id, agent["name"], session_key, message, request_id), daemon=True)
    worker.start()
    return {"ok": True, "accepted": True, "pending": True, "requestId": request_id, "agentId": agent_id, "agentName": agent["name"], "sessionKey": session_key, "history": load_history().get(agent_id, [])[-80:]}


def send_recovery_nudge(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    agent = next(a for a in AGENTS if a["id"] == agent_id)
    session_key = f"agent:{agent_id}:observer-site"
    message = (
        "관제 화면에서 현재 작업이 지연 가능 상태로 표시됩니다. "
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
    key = agent.get("latestSessionKey") if agent else f"agent:{agent_id}:observer-site"
    messages_payload = gateway_call("sessions.get", {"key": key, "limit": 20}, timeout=18)
    messages = messages_payload.get("messages") if isinstance(messages_payload, dict) else []
    if not isinstance(messages, list):
        messages = []
    compact_messages = []
    for msg in messages[-20:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("type") or "message"
        content = msg.get("content") or msg.get("text") or msg.get("message") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)[:2000]
        compact_messages.append({"role": role, "content": content[:2000], "ts": msg.get("ts") or msg.get("timestamp") or msg.get("createdAt")})
    return {"ok": True, "agentId": agent_id, "sessionKey": key, "agent": agent, "messages": compact_messages}


def abort_agent_session(agent_id: str) -> dict[str, Any]:
    if agent_id not in {a["id"] for a in AGENTS}:
        raise ValueError("허용되지 않은 에이전트입니다.")
    summary = summarize_agents()
    agent = next((a for a in summary.get("agents", []) if a.get("id") == agent_id), None)
    key = agent.get("latestSessionKey") if agent else f"agent:{agent_id}:observer-site"
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
            state = "lag" if stale_seconds >= 180 else "working"
        elif normalized == "warning":
            state = "failed"
        item = {
            "key": key,
            "state": state,
            "status": status,
            "staleSeconds": stale_seconds,
            "model": session.get("model"),
            "tokens": int(float(session.get("totalTokens") or 0) or 0),
        }
        subs.append(item)
    subs.sort(key=lambda item: item.get("staleSeconds", 0))
    recent = subs[:12]
    return {
        "total": len(subs),
        "recent": recent,
        "done": sum(1 for item in subs if item["state"] == "done"),
        "working": sum(1 for item in subs if item["state"] == "working"),
        "lag": sum(1 for item in subs if item["state"] == "lag"),
        "failed": sum(1 for item in subs if item["state"] == "failed"),
    }


def summarize_agents() -> dict[str, Any]:
    now = int(time.time() * 1000)
    sessions_data = gateway_call("sessions.list", timeout=18)
    sessions = sessions_data.get("sessions") if isinstance(sessions_data, dict) else []
    if not isinstance(sessions, list):
        sessions = []

    output = []
    for base in AGENTS:
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

        state = "working" if recent_active else "idle"
        current_started = to_epoch_ms(current.get("startedAt") or current.get("updatedAt")) if current else 0
        current_updated = to_epoch_ms(current.get("updatedAt") or current.get("startedAt")) if current else 0
        age_ms = max(0, now - current_started) if current_started else 0
        stale_ms = max(0, now - current_updated) if current_updated else 0
        raw_status = str(current.get("status") or "unknown") if current else "none"

        # 현재 작업중 여부는 running/queued/processing 세션을 우선한다.
        # 실행 중 세션의 갱신 지연만으로는 점검 처리하지 않는다.
        # 점검은 명시적인 실패/오류/중단 상태가 최근에 관측될 때만 표시한다.
        is_lagging = bool(recent_active and stale_ms >= 180_000)
        if not active and normalize_status(raw_status) == "warning" and stale_ms < 120_000:
            state = "warning"

        latest_updated = current_updated
        status_text = "응답 가능"
        if state == "working":
            status_text = "작업 중"
        elif state == "warning":
            status_text = "점검 필요"

        detail = "현재 생성 중 아님 · 최근 실행 세션 없음"
        if current:
            total_tokens = int(float(current.get("totalTokens") or 0) or 0)
            model = current.get("model") or "unknown"
            if recent_active:
                lag_note = " · 응답 지연 가능" if is_lagging else ""
                detail = f"현재 작업 중: {raw_status} · 마지막 갱신 {stale_ms // 1000:,}초 전{lag_note} · {model} · {total_tokens:,} tokens"
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
            "isLagging": is_lagging,
            "ageSeconds": age_ms // 1000 if current else 0,
            "staleSeconds": stale_ms // 1000 if current else 0,
            "latestUpdatedAt": latest_updated,
            "latestSessionKey": current.get("key") if current else None,
            "latestPreview": current.get("lastMessagePreview") if current else None,
            "subagents": subagent_summary,
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
        return "작업중 · 지연 가능" if agent and agent.get("isLagging") else "작업중"
    if state == "warning":
        return "점검"
    return "대기"


def state_badge_class(state: str, agent: dict[str, Any] | None = None) -> str:
    if state == "working":
        return "lag" if agent and agent.get("isLagging") else "work"
    if state == "warning":
        return "warn"
    return "idle"


def render_agent_card(agent: dict[str, Any]) -> str:
    state = str(agent.get("state") or "idle")
    name = html_escape(agent.get("name") or agent.get("id") or "agent")
    orb = html_escape(agent.get("orb") or name[:1])
    detail = html_escape(agent.get("detail") or agent.get("statusText") or "상태 확인 중")
    working_tag = f"실행중 {agent.get('activeSessionCount') or 1}" if agent.get("isWorkingNow") else "현재 생성 중 아님"
    lag_tag = "응답 지연 가능" if agent.get("isLagging") else None
    tags = [agent.get("role"), working_tag, lag_tag, *(agent.get("tags") or []), agent.get("id")]
    tag_html = "".join(f'<span class="tag">{html_escape(tag)}</span>' for tag in tags[:6] if tag)
    subagents = agent.get("subagents") or {}
    dots = "".join(f'<span class="sub-dot {html_escape(item.get("state"))}" title="{html_escape(item.get("status"))} · {html_escape(item.get("staleSeconds"))}초 전"></span>' for item in (subagents.get("recent") or [])[:12])
    sub_html = ""
    if subagents.get("total"):
        sub_html = f'<div class="subagent-strip"><div class="subagent-dots">{dots}</div><div class="subagent-text">하위 작업 {html_escape(subagents.get("total"))} · 진행 {html_escape(subagents.get("working"))} · 지연 {html_escape(subagents.get("lag"))} · 실패 {html_escape(subagents.get("failed"))}</div></div>'
    badge = state_label(state, agent)
    badge_class = state_badge_class(state, agent)
    work_class = "" if state == "idle" else "work"
    lag_class = "lag" if agent.get("isLagging") else ""
    warn_class = "warn" if state == "warning" else ""
    accent = "idle" if state == "idle" else "work"
    return f"""
            <article class="agent {work_class} {lag_class} {warn_class}" role="button" tabindex="0" data-agent="{name}" data-state="{html_escape(agent.get('statusText') or '상태 확인 중')}" data-accent="{accent}" data-agent-id="{html_escape(agent.get('id'))}">
              <span class="state-badge {badge_class}">{badge}</span>
              <div class="orb">{orb}</div>
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
        active_html = "\n".join(render_agent_card(a) for a in active) or '<article class="agent"><div class="orb">✓</div><div><div class="name">작업중 없음</div><div class="status">현재 실행 중인 에이전트가 없습니다.</div></div></article>'
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
        if path == "/api/chat/history":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            agent_id = (qs.get("agentId") or ["observer"])[0]
            if agent_id not in {a["id"] for a in AGENTS}:
                self.json_response({"ok": False, "error": "허용되지 않은 에이전트입니다."}, 400)
                return
            self.json_response(public_history(agent_id))
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
                result = send_chat(agent_id, message)
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
            self.json_response({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8788"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"observer-site serving on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
