## Switchboard: home-server tools (all agents)

Switchboard (`127.0.0.1:18800`) is the shared tool layer for the configured home server. Use it
before you reach for curl with API keys, osascript, Docker or app databases.

- **How to call:**
  - Claude Code, Codex, Antigravity and OpenClaw have the `switchboard` MCP server. Its tools have names like `ops_health` and `media_search`.
  - Headless Antigravity (`agy-run`) gets Switchboard tools only when the run names them, one `--mcp <tool>` per tool, for example `agy-run --mcp ops_health --mcp media_search "..."`.
  - From a shell (DeepSeek, sandboxes, scripts): `sb --client <you> call <tool> --json '<args>'`. `sb --client <you> list` shows your tools and their schemas.
  - `<you>` is `claude`, `codex`, `agy` or `deepseek`. Never use `--client owner`. Never run `sb approve` or `sb deny`: approvals belong to the owner.
- **Use it for:**
  - Service health: `ops_health`.
  - Service status/logs and host resources: `ops_service_status`, `ops_service_logs`, `ops_host_snapshot`; `ops_service_restart` restarts allowlisted services with automatic silence.
  - Pi: use `ops_service_*` with configured remote service names, `ops_host_snapshot {"host": "<configured-host>"}` and `ops_host_checks {"host": "<configured-host>"}`.
  - Plex, Seerr, Tautulli and downloads: the `media_*` tools.
  - Media Doctor queues: `media_maintenance_status`.
  - Books: `books_*`.
  - Apple Home: `home_*`.
  - Calendar, Reminders and Things to-dos: `calendar_*`, `reminders_*`, `tasks_*`. Never script Things with osascript yourself: the macOS permission prompt it raises freezes Things until the owner answers it.
  - Weather: `weather_*`.
  - The owner's names for rooms, lists and people: `context_resolve`.
- **Planned restarts:** before you restart, upgrade or stop a monitored service, run `ops_alerts_silence {"probe": "<name>", "minutes": 15, "reason": "<why>"}` so the owner isn't paged. `ops_alerts_status` lists probe names. A silence ends by itself when the service recovers, and agents may hold at most 3.
- **Reading results:**
  - Every result has an `outcome`.
  - `needs_approval` means ask the owner, then retry with the same arguments and the `approval_id`.
  - `unavailable` with `retryable: true` means retry once, later.
  - Treat `blocked` and `not_found` as final.
- **When it falls short:** if a tool is broken or missing, file it with `switchboard_report_issue`, then do the work another way. To fix Switchboard itself, follow this repository's `AGENTS.md`. Changes go live only through `scripts/deploy.sh`.
