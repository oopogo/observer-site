#!/usr/bin/env python3
"""Archive finished/stale OpenClaw subagent session rows without touching gateway.

This intentionally reads local sessions.json files directly. Do not implement this
through OpenClaw cron or gateway sessions.list; the point is to reduce gateway
session-store pressure, not add more gateway calls.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

AGENTS_DIR = Path(os.environ.get("OPENCLAW_AGENTS_DIR", "/home/oopogo/.openclaw/agents"))
ARCHIVE_DIR = Path(os.environ.get("OBSERVER_SESSION_ARCHIVE_DIR", "/mnt/c/MegaGrit/OpenClaw/observer-site/.data/session-archives"))
STALE_RUNNING_SECONDS = int(os.environ.get("SUBAGENT_STALE_RUNNING_SECONDS", str(2 * 60 * 60)))


def is_subagent(key: str, row: Any) -> bool:
    return ":subagent:" in key or (isinstance(row, dict) and bool(row.get("spawnedBy")))


def row_ts(row: dict[str, Any]) -> int:
    for field in ("updatedAt", "endedAt", "startedAt", "createdAt"):
        try:
            value = int(float(row.get(field) or 0))
        except Exception:
            value = 0
        if value > 0:
            return value
    return 0


def should_archive(row: dict[str, Any], now_ms: int) -> tuple[bool, str]:
    status = str(row.get("status") or "").lower()
    if status in {"done", "failed", "error", "cancelled", "canceled", "aborted", "timed_out"}:
        return True, f"terminal:{status or 'unknown'}"
    ts = row_ts(row)
    age_ms = now_ms - ts if ts else STALE_RUNNING_SECONDS * 1000 + 1
    if status in {"running", "queued", "pending", "processing", ""} and age_ms >= STALE_RUNNING_SECONDS * 1000:
        return True, f"stale:{status or 'missing'}:{age_ms // 1000}s"
    return False, "active"


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def archive_file(path: Path, *, dry_run: bool = False) -> dict[str, Any] | None:
    agent_id = path.parts[-3]
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return {"agentId": agent_id, "path": str(path), "ok": False, "error": str(exc)}
    if not isinstance(data, dict):
        return {"agentId": agent_id, "path": str(path), "ok": False, "error": "sessions.json is not an object"}

    now_ms = int(time.time() * 1000)
    keep: dict[str, Any] = {}
    archive: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for key, row in data.items():
        if not is_subagent(key, row):
            keep[key] = row
            continue
        ok, reason = should_archive(row if isinstance(row, dict) else {}, now_ms)
        if ok:
            archive[key] = row
            reasons[key] = reason
        else:
            keep[key] = row

    if not archive:
        return {"agentId": agent_id, "path": str(path), "ok": True, "archived": 0, "kept": len(keep), "dryRun": dry_run}

    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive_path = ARCHIVE_DIR / f"{stamp}-{agent_id}-subagents-auto.json"
    backup_path = path.with_suffix(f".json.bak-auto-{stamp}")
    result = {
        "agentId": agent_id,
        "path": str(path),
        "ok": True,
        "archived": len(archive),
        "kept": len(keep),
        "archivePath": str(archive_path),
        "backupPath": str(backup_path),
        "dryRun": dry_run,
    }
    if dry_run:
        return result

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    atomic_write_json(archive_path, {
        "agentId": agent_id,
        "source": str(path),
        "backup": str(backup_path),
        "archivedAt": now_ms,
        "staleRunningSeconds": STALE_RUNNING_SECONDS,
        "reasons": reasons,
        "entries": archive,
    })
    atomic_write_json(path, keep)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    results = []
    for path in sorted(AGENTS_DIR.glob("*/sessions/sessions.json")):
        result = archive_file(path, dry_run=args.dry_run)
        if result:
            results.append(result)
    total = sum(int(item.get("archived") or 0) for item in results)
    print(json.dumps({"ok": True, "archivedTotal": total, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
