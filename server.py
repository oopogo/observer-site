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
        latest = agent_sessions[0] if agent_sessions else None
        current = active[0] if active else latest

        state = "working" if active else "idle"
        current_started = to_epoch_ms(current.get("startedAt") or current.get("updatedAt")) if current else 0
        current_updated = to_epoch_ms(current.get("updatedAt") or current.get("startedAt")) if current else 0
        age_ms = max(0, now - current_started) if current_started else 0
        stale_ms = max(0, now - current_updated) if current_updated else 0
        raw_status = str(current.get("status") or "unknown") if current else "none"

        # 현재 작업중 여부는 running/queued/processing 세션만 기준으로 삼는다.
        # 과거 failed/done 세션은 최근 기록으로만 보여주고, 현재 상태를 덮어쓰지 않는다.
        if active and stale_ms >= 180_000:
            state = "warning"
        elif not active and normalize_status(raw_status) == "warning" and stale_ms < 120_000:
            state = "warning"

        latest_updated = current_updated
        status_text = "대기 중"
        if state == "working":
            status_text = "작업 중"
        elif state == "warning":
            status_text = "점검 필요"

        detail = "현재 작업 없음 · 최근 실행 세션 없음"
        if current:
            total_tokens = int(float(current.get("totalTokens") or 0) or 0)
            model = current.get("model") or "unknown"
            if active:
                detail = f"현재 작업 중: {raw_status} · 마지막 갱신 {stale_ms // 1000:,}초 전 · {model} · {total_tokens:,} tokens"
            else:
                detail = f"현재 작업 없음 · 최근 세션: {raw_status} · {model} · {total_tokens:,} tokens"

        output.append({
            **base,
            "state": state,
            "statusText": status_text,
            "detail": detail,
            "sessionCount": len(agent_sessions),
            "activeSessionCount": len(active),
            "isWorkingNow": bool(active),
            "ageSeconds": age_ms // 1000 if current else 0,
            "staleSeconds": stale_ms // 1000 if current else 0,
            "latestUpdatedAt": latest_updated,
            "latestSessionKey": current.get("key") if current else None,
            "latestPreview": current.get("lastMessagePreview") if current else None,
        })

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
        if path != "/api/chat/send":
            self.json_response({"ok": False, "error": "not found"}, 404)
            return
        try:
            data = read_body_json(self)
            agent_id = str(data.get("agentId") or "observer")
            message = str(data.get("message") or "")
            result = send_chat(agent_id, message)
            self.json_response(result, 200 if result.get("ok") else 504)
        except Exception as exc:
            self.json_response({"ok": False, "error": str(exc)}, 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8788"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"observer-site serving on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
