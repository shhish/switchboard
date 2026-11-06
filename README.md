# Switchboard

**Agent harnesses come and go. Your home's tools, rules and context shouldn't have to.**

## The problem

If you run a home server and use AI agents, you probably use more than one:

- Claude Code or Codex in a terminal;
- a chat assistant on your phone;
- a voice gadget on the counter;
- whichever new harness launched this month.

Each one wants its own way to reach your stuff, and each one has to be taught again:

- which Plex library is which, and which API key goes where;
- that requests go through Seerr and never straight to Sonarr;
- that the garage door needs a confirmation;
- that one service is slow because its data sits on a USB disk, and isn't actually down.

That knowledge ends up scattered across prompt files, MCP configs, shell history and one-off scripts. It drifts apart, it leaks credentials into places they shouldn't be, and it's gone the day you switch tools. Meanwhile every agent improvises with `curl`, `docker logs` and `ssh`, does the same chores by hand over and over, and has as much access as your shell does.

The harness is the part that changes fastest. What should stay put is everything else:

- **the capabilities:** what can actually be done;
- **the rules:** who may do what, and what needs a human's yes;
- **the context:** names, preferences and conventions;
- **the operational memory:** what's running, what broke, and what was tried.

## The idea

Switchboard is that stable layer. It's one local service on your server, and every agent plugs into the same typed capabilities through whichever door it supports: MCP, a JSON CLI, or HTTP on loopback.

- **Write a capability once and every agent gets it.** Today's agent, tomorrow's and your phone assistant all call the same `media_search`, `home_set` or `ops_service_logs`, with the same inputs, outputs and error shapes.
- **Keep policy in one place, not in prompts.** Each agent gets its own token and scopes. Sensitive actions need an exact, single-use approval, which only the owner (or a client explicitly trusted to collect the owner's confirmation) can grant. Every call is audited, and secrets stay in owner-only files the agents never see.
- **Keep context in one place too.** Shared owner context resolves your names for things: "living room lights", "the bedroom TV", your Things projects. Skills explain the workflows in plain text, and one rules file is synced into every agent's global instructions on deploy, so a new harness learns the house in seconds.
- **Let it maintain itself.** It monitors your services and alerts you by phone and email. Slow-but-alive services can get longer timeouts and higher failure thresholds so they don't page you. A dead-man heartbeat to a second machine catches the case where the whole server dies. Agents report broken or missing tools to a shared issue queue, and they're expected to improve Switchboard itself: it's built to keep evolving, not to be set up once.
- **Swap harnesses freely.** When a new agent tool appears, register one MCP server or point it at the CLI, give it its own token and scopes, and it inherits everything.

It started as one person's home setup, a Mac mini and a Raspberry Pi shared by Claude Code, Codex, Antigravity, ChatGPT and a small ESP32 voice device. If you're juggling several agents on one box, you probably have the same problem.

## What's in the box

- **Core:** a Python service on `127.0.0.1:18800` with a typed registry. Per-client scopes, resource allowlists, approvals, jobs, durable state and a hash-chained audit log (queryable by agents: what ran, why, and what it caused) apply the same way to MCP (stateless 2026-07-28, plus legacy initialization), HTTP and the CLI. A stdio bridge serves harnesses that only speak stdio.
- **Integrations (each optional):**
  - service health probes, monitoring and alerts with self-ending silences;
  - service status, logs and restarts, plus host snapshots;
  - Plex, Tautulli and Seerr reads and requests;
  - LazyLibrarian books;
  - Apple Home, through a HomeKit relay app;
  - Calendar and Reminders (EventKit) and Things 3;
  - NWS weather;
  - read-only search over a Markdown notes vault (Obsidian-style), and a recipe library;
  - PlayStation 5 power, game library, launch and close;
  - "tell me when" watches that notify once by push or email;
  - shared owner context, an issue queue and federation to an MCP device hub.
- **Native macOS helper (optional):** a small signed app that holds the macOS privacy grants that a launchd Python service can't get: Calendar, Reminders, Things automation and Local Network. It answers a fixed set of requests over a Unix socket.
- **Remote host agent (optional):** one fixed command set reached over an SSH forced command for a second machine, such as a Raspberry Pi. It covers status, logs, diagnostics, allowlisted restarts and package-list refreshes, with no shell.
- **Skills and agent rules:** plain-text workflow guides, linked into each agent's skills folder on deploy.

Each integration needs its own backend; this is not a bundle of those services.

See the [capability catalog](docs/CAPABILITIES.md), [example config](config/config.example.yaml),
[example policy](config/policy.example.yaml), [contributor guide](AGENTS.md),
[roadmap](docs/ROADMAP.md) and [workflow skills](skills/).

## Quick start

Requires Python 3.13+; the native helper additionally needs macOS and the Swift
build tools. Runtime Python dependencies are aiohttp and PyYAML.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
mkdir -p ~/.config/switchboard
chmod 700 ~/.config/switchboard
cp config/config.example.yaml ~/.config/switchboard/config.yaml
cp config/policy.example.yaml ~/.config/switchboard/policy.yaml
chmod 600 ~/.config/switchboard/config.yaml ~/.config/switchboard/policy.yaml
```

Edit those private files first: remove sample probes you do not run, disable
unused backends and reduce client scopes and allowlists to what you need.
Weather is disabled without a location; enabling it requires `name`, a five-digit
`zip`, numeric `lat` and `lon`. Tools use the configured location. Maintenance
snapshots and compression are disabled without explicit deployment paths.
Secret config values are file paths, never inline credentials.

Provision only the client identities you intend to use:

```sh
.venv/bin/sb token create owner
.venv/bin/sb token create codex
.venv/bin/sb serve
```

Token creation prints a path, never the credential. Tokens are owned regular
0600 files in a 0700 directory. Removing a token revokes it on the next request;
`sb token list` lists identities and `sb token revoke codex` revokes one.

In another terminal:

```sh
.venv/bin/sb --client codex list
.venv/bin/sb --client codex call ops_health --json '{}'
.venv/bin/sb doctor
.venv/bin/sb manifest
```

CLI calls default to the owner credential. Agents must select their own identity
with `--client`; the flag selects a token, not a claimed server-side identity.
The stdio adapter requires an explicit client:

```sh
~/.local/share/switchboard/current/.venv/bin/sb mcp-stdio --client codex
```

Register that deployed absolute executable path and arguments in your agent's
MCP configuration. See [Dot integration](docs/DOT.md) for the tunnel workflow.
No public listener is needed. Skills are guidance and grant no permissions.

Configuration defaults to `~/.config/switchboard/`; override with
`SWITCHBOARD_CONFIG_DIR`. State defaults to `~/Library/Application Support/switchboard`
and logs to `~/Library/Logs/switchboard`, overridable with `SWITCHBOARD_STATE_DIR`
and `SWITCHBOARD_LOG_DIR`. Runtime directories must be 0700. No policy file means
no authorized calls, including for owner.

## Deployment on macOS

Use a reviewed committed revision for deployment. `scripts/deploy.sh` requires a
clean checkout, runs checks, builds an immutable release, switches `current`
atomically, restarts the configured launchd job and checks health. It restores
the previous release on a failed health check. The newest five releases are kept.
It also links skills into supported agent directories and runs
`scripts/sync-agent-rules.py`, which updates marked blocks in agent instruction
files under `Path.home()`. Review those targets before first deployment.

Both deployment and native-build scripts source the optional private
`~/.config/switchboard/deploy.env`. It must be an owned regular non-symlink file
with mode **0600**, otherwise the scripts refuse it. It is shell code; only the
owner should edit it. For example:

```sh
SWITCHBOARD_LABEL=com.example.switchboard
SWITCHBOARD_EVENTKIT_LABEL=com.example.switchboard.eventkit
SWITCHBOARD_BUNDLE_ID=com.example.switchboard.eventkit
SWITCHBOARD_SIGN_IDENTITY='Developer ID Application: <identity>'
SWITCHBOARD_PYTHON=/absolute/path/to/python3
```

The service label defaults to `com.example.switchboard`. The deployment Python
default is pinned in `scripts/deploy.sh`; override it for your installed runtime.
`SWITCHBOARD_DEPLOY_ROOT` optionally replaces `~/.local/share/switchboard`.
The helper label is for owner installation of the helper template; deploy does
not install or restart the helper. Existing deployments should keep their private
labels, bundle ID and signing identity to preserve launchd and macOS privacy grants.

Copy [the service template](launchd/switchboard.plist.example) to a private file
under `~/Library/LaunchAgents/<label>.plist`. Replace `__HOME__`, `__RELEASE__`
(the stable `current` path) and `__LABEL__` with absolute values. The template
passes the configured label into the service environment for self-restart
protection. launchd does not expand `~`. Create the private config and log
directories with the required modes, then install the reviewed job with
`launchctl bootstrap gui/$(id -u) <absolute-plist-path>` and kickstart its label.

```sh
scripts/deploy.sh
scripts/deploy.sh --status
scripts/deploy.sh --rollback
```

Only the deployment script should change the running Python release. MCP
registrations, launchd and skill symlinks must point at `current`, not a checkout.

## Optional native helper and remote host agent

Build the helper with `scripts/build-eventkit.sh`. Set a stable signing identity
and bundle ID in private deployment settings before installing it. The script
sets the built app's bundle ID before signing; the source plist stays generic.
Ad-hoc signing is available for development but re-signing can invalidate grants.
The app is built under `native/build/Switchboard EventKit.app`.

Use [the helper template](launchd/switchboard-eventkit.plist.example) with its own
`__LABEL__` and an absolute `__RELEASE__` path containing the built app. Enable
`backends.eventkit` only after installing the helper. Calendar/Reminders access,
Things Automation and Local Network permission are independent macOS grants.
Only the local owner, present to answer prompts, may run:

```sh
sb eventkit status
sb eventkit request-access
sb things status
sb things request-access
sb net request-access
```

The helper uses an owned 0600 Unix socket in a 0700 directory and checks same-UID
peers. Things can be enabled independently and uses the same socket. Its fixed
compiled AppleScript receives descriptor arguments; user text is not source code.
Task writes require policy destinations; checklist and Evening creation are
currently unsupported. See the [task workflow](skills/switchboard-tasks/SKILL.md).

RFC1918 probes route through the helper when enabled; loopback probes stay in
Python. Privacy denial produces `unknown` rather than a false device-down alert.
For remote hosts, follow [the Pi agent setup](pi-agent/README.md): fixed requests,
restricted SSH keys, pinned host keys and no shell, forwarding, pairing or reboot.

## Monitoring and results

`ops_health` probes the configured non-finance service list concurrently and
caches results for 15 seconds. HTTP below 500 is `up`, 500+ is `degraded`, and
connection/TLS/timeouts are `down`. Continuous monitoring is opt-in and persists
incident state, reminders, recovery notifications and delivery retries. Configure
ntfy and/or STARTTLS email with private secret files and explicit delivery-host
allowlists. An optional private heartbeat URL file sends a native RFC1918 ping.
Doctor checks setup without sending notifications.

Before planned maintenance, use `ops_alerts_silence` with the configured probe;
service-restart tools automatically silence linked probes and enforce a durable
cooldown. `ops_alerts_status` reports incidents, silences and delivery backlog.

Results include `status`, `outcome`, `source` and `as_of`; lists report truncation.
Acceptance means a request/job was accepted, not that downstream work finished.
Poll returned job IDs. Weather reports observation time and stale cache age;
external prose is untrusted data. See each skill for workflow-specific limits.

## Security model

- Authentication derives identity from the bearer token. Every call, even to a
  tool hidden from discovery, validates a closed schema and current policy.
  Unknown or missing policy fails closed. Scopes and resource rules both apply.
- Approval grants bind the exact client, capability and canonical arguments,
  expire, and are atomically consumed before execution, including failures.
  Notification answers alone grant nothing. Only owner CLI resolution or an
  explicitly trusted client's confirmation path resolves a grant.
- Every capability call is audited in SQLite and redacted JSONL. Credentials,
  request headers, backend bodies and arbitrary exception text must not be logged.
- The server rejects non-loopback peers/Host and Origin headers. Local backend
  URLs use HTTP literal loopback addresses. Probes accept loopback and RFC1918;
  remote hosts additionally accept RFC 6598 shared space. No broader private-IP
  classification is used. Backend redirects and environment proxies are disabled;
  NWS has a separate fixed HTTPS origin and bounded same-origin redirects.
- No generic shell/exec, raw HTTP passthrough, finance/trading, credential exposure,
  Docker socket, device pairing, OTA or reboot capabilities are permitted.
- The owner controls policy, tokens and deployed binaries. A process with that
  user's filesystem access can bypass this boundary. Routine clients should get
  only their own credential and the public adapters.

## Development and publication

```sh
PYTHONPATH=src .venv/bin/sb manifest --markdown > docs/CAPABILITIES.md
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format src tests
(cd native/SwitchboardEventKit && swift test --cache-path ../build/cache --disable-sandbox)
```

Tests use fake backends and temporary files, never live mutations or services.
See [AGENTS.md](AGENTS.md) for registry, policy and schema requirements.

Nothing personal belongs in the public repository: no home addresses, owner
names, credentials, personal filesystem paths or deployment identifiers. Use
private configuration and `AGENTS.local.md` for deployment notes. Snapshot
publication honors `.oss-export-exclude`; private history is not the public tree.
Run the publication privacy checker on the export before publishing.

Licensed under [MIT](LICENSE).
