# observer-site

Operations cockpit for OpenClaw agents at https://observer.megagrit-ai.com/.

## MCP/ACP contract

The site exposes MCP-style JSON envelopes so the web UI, observer, and ACP agents can share one status contract before a dedicated MCP server is split out.

### `GET /api/mcp/status`

Returns:

```json
{
  "ok": true,
  "mcp": {
    "schema": "observer.mcp.status.v1",
    "tool": "observer.status.snapshot",
    "generatedAt": 0,
    "summary": {
      "agents": 3,
      "working": 0,
      "assigned": 0,
      "reporting": 0,
      "reportMissing": 0,
      "completedReportMissing": 0,
      "silenceWarning": 0,
      "opsCritical": 0,
      "opsWarning": 0,
      "acpActive": 0,
      "acpStale": 0,
      "acpNeedsRecovery": 0,
      "acpNeedsReportCheck": 0
    },
    "agents": [],
    "opsAlerts": {},
    "acpSessions": {}
  }
}
```

`/api/agents` includes the same `mcp` object for UI reuse.

### ACP tool envelopes

ACP action APIs keep their legacy top-level fields and additionally return `mcpTool`:

```json
{
  "schema": "observer.mcp.tool_result.v1",
  "tool": "observer.acp.detail",
  "generatedAt": 0,
  "ok": true,
  "result": {}
}
```

Current tools:

- `observer.acp.detail` via `POST /api/acp/detail` with `{ "recordId": "..." }`
- `observer.acp.recover` via `POST /api/acp/recover` with `{ "recordId": "..." }`
- `observer.acp.abort` via `POST /api/acp/abort` with `{ "recordId": "..." }`
- `observer.acp.cleanup_stale` via `POST /api/acp/cleanup-stale`
- `observer.acp.spawn` via `POST /api/acp/spawn`

### Lifecycle fields

`acpSessions.lifecycle` summarizes session state:

- `running`: ACP pid alive and not closed
- `stale`: not closed but pid missing
- `closed`: closed or `closed_at` present
- `needsRecovery`: running + stale
- `needsReportCheck`: closed sessions with captured messages that may need result review

Report-debt fields:

- `summary.reportMissing`: agent-level report debt count
- `summary.completedReportMissing`: activeWork completed but current chat report may be missing
- `summary.silenceWarning`: visible report silence warning count
