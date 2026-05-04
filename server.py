#!/usr/bin/env python3
"""Observer site read-only backend.

Serves the static homepage and a small /api/agents endpoint that summarizes
OpenClaw session state for visual monitoring. No command execution endpoint is
exposed here.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "/home/oopogo/.npm-global/bin/openclaw")
AGENTS = [
    {"id": "main", "name": "밀레느", "orb": "밀", "role": "메인 작업 에이전트"},
    {"id": "observer", "name": "observer", "orb": "옵", "role": "운영 감시자"},
    {"id": "mediacontentproducer", "name": "미디어", "orb": "미", "role": "콘텐츠 프로듀서"},
]


def parse_json_suffix(raw: str) -> Any:
    cleaned = raw.strip()
    starts = [i for i, ch in enumerate(cleaned) if ch in "{["]
    for start in reversed(starts):
        try:
            return json.loads(cleaned[start:])
        except Exception:
            continue
    raise ValueError(f"No JSON payload in output: {cleaned[:300]}")


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


def summarize_agents() -> dict[str, Any]:
    now = int(time.time() * 1000)
    sessions_data = gateway_call("sessions.list", timeout=8)
    sessions = sessions_data.get("sessions") if isinstance(sessions_data, dict) else []
    if not isinstance(sessions, list):
        sessions = []

    output = []
    for base in AGENTS:
        agent_sessions = [s for s in sessions if isinstance(s, dict) and (s.get("agentId") == base["id"] or str(s.get("key", "")).startswith(f"agent:{base['id']}:") )]
        agent_sessions.sort(key=lambda s: to_epoch_ms(s.get("updatedAt") or s.get("startedAt")), reverse=True)
        active = [s for s in agent_sessions if normalize_status(str(s.get("status") or "")) == "working"]
        failed = [s for s in agent_sessions if normalize_status(str(s.get("status") or "")) == "warning"]
        latest = active[0] if active else (failed[0] if failed else (agent_sessions[0] if agent_sessions else None))

        state = "idle"
        if active:
            state = "working"
        if failed:
            state = "warning"
        if latest and state == "working":
            started = to_epoch_ms(latest.get("startedAt") or latest.get("updatedAt"))
            age_ms = max(0, now - started) if started else int(float(latest.get("ageMs") or 0) or 0)
            context_tokens = int(float(latest.get("contextTokens") or 0) or 0)
            if age_ms >= 120_000 or context_tokens >= 180_000:
                state = "warning"

        latest_updated = to_epoch_ms(latest.get("updatedAt")) if latest else 0
        status_text = "대기 중"
        if state == "working":
            status_text = "작업 중"
        elif state == "warning":
            status_text = "확인 필요"

        detail = "최근 실행 세션 없음"
        if latest:
            total_tokens = int(float(latest.get("totalTokens") or 0) or 0)
            model = latest.get("model") or "unknown"
            raw_status = latest.get("status") or "unknown"
            detail = f"최근 세션: {raw_status} · {model} · {total_tokens:,} tokens"

        output.append({
            **base,
            "state": state,
            "statusText": status_text,
            "detail": detail,
            "sessionCount": len(agent_sessions),
            "activeSessionCount": len(active),
            "latestUpdatedAt": latest_updated,
            "latestSessionKey": latest.get("key") if latest else None,
            "latestPreview": latest.get("lastMessagePreview") if latest else None,
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
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path.split("?", 1)[0] == "/api/agents":
            try:
                payload = summarize_agents()
                code = 200
            except Exception as exc:  # keep UI alive with a clear read-only error
                payload = {"ok": False, "error": str(exc), "generatedAt": int(time.time() * 1000), "agents": [], "counts": {"total": 0, "idle": 0, "working": 0, "warning": 0}}
                code = 502
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8788"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"observer-site serving on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
