# Setting up Switchboard

Everything here is optional beyond the core service. Start with the quick start, then add the native helper, the remote host agent and monitoring as you need them.

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
MCP configuration. See [Dot integration](DOT.md) for the tunnel workflow.
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

Copy [the service template](../launchd/switchboard.plist.example) to a private file
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

Use [the helper template](../launchd/switchboard-eventkit.plist.example) with its own
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
currently unsupported. See the [task workflow](../skills/switchboard-tasks/SKILL.md).

RFC1918 probes route through the helper when enabled; loopback probes stay in
Python. Privacy denial produces `unknown` rather than a false device-down alert.
For remote hosts, follow [the Pi agent setup](../pi-agent/README.md): fixed requests,
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

## Shared memory in agent sessions

The owner enables `memory.enabled` in private configuration and runs the reviewed
`scripts/owner-grant-agent-access.sh` to provision memory grants and the separate
`dream` token. No hook or example installs itself. Existing `context_*` tables
remain independent.

Shell-only agents can start a session with:

```sh
sb --client codex memory brief --domain ops
sb memory brief --client deepseek --json
```

Plain output contains only the brief. Disabled, unreachable or unavailable memory
produces empty output and exit 0, with a three-second request deadline. `--json`
returns the result object (an empty brief on failure).

For Claude Code, the owner can merge this into `~/.claude/settings.json`, preserving
existing hooks. The command refers to the reviewed deployed release:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.local/share/switchboard/current/scripts/claude-session-brief.sh\""
          }
        ]
      }
    ]
  }
}
```

The hook emits `hookSpecificOutput.additionalContext` and kills its `sb` subprocess
after three seconds; all failures are silent. See the
[Claude Code hook reference](https://code.claude.com/docs/en/hooks#sessionstart).

Entity tools declare `Capability.memory=MemorySubject(...)` beside their input
and output contract. The registry adds optional top-level `notes` and enforces
an explicit declaration on every tool. Entity-shaped inputs require a subject
or a written `memory_optout` reason. Selectors walk only declared paths and have
fixed breadth/depth limits. Notes contain at most three active readable records.
Weather, note and recipe subjects use `general`; Reminders subjects use `tasks`,
within the existing memory domain map. Notes remain data, may be stale, and grant
no permission. Verify a contradiction and supersede the obsolete memory.

## Memory maintenance and reflection

Hourly housekeeping runs deterministic tidy once per local day after 03:00.
It records duplicate, contradictory, stale and unused records, and proposals that
have waited at least seven days. Tidy never edits memories. Resolved findings
stay in the database; identical findings are suppressed until their records
change. Operational usage counters retain 35 days and incident observations
seven days; memory records and edit history remain durable.

At `memory.digest_hour` (default 09:00 local), an open proposal or finding queues
one daily owner digest through configured ntfy/email outbox channels. No channels
means no notification. Doctor reports counts and the last tidy, digest and
successful dream-import times; `audit_digest.memory` reports reads, delivered
notes, write landing statuses and owner review counts.

```sh
sb memory review
sb memory review --keep-finding FINDING_ID
sb memory review --retract-finding FINDING_ID
sb memory review --supersede-finding FINDING_ID --with-id ACTIVE_MEMORY_ID
```

The owner may install `launchd/memory-dream.plist.example` as a separate nightly
job after reviewing it: replace `__HOME__` and `__LABEL__`, save the resulting
plist under `~/Library/LaunchAgents/`, and load it with the owner's usual launchd
workflow. The example runs the deployed `scripts/memory-dream.sh` at 04:15;
Switchboard does not install, load or modify it.

The runner fetches a redacted JSON input of at most 40 KB into a private temporary
brief. It calls `delegate ds` as a read-only job with no handback contract, falls
back once to `delegate codex` only for exit 4 (quota), validates an array of at most
15 memories, and imports proposals labeled `dream:YYYY-MM-DD`. Failures produce
one fixed log line and exit 0; there is no retry loop. It never mines transcripts.
Model calls happen in this owner-installed runner, outside the capability server.

`memory:reflect` exposes only domain-filtered audit metadata, monitoring/storage
observations, routine summaries and active memory titles; no memory bodies or raw
audit arguments. Every proposed reflection item needs an exact evidence reference
issued in that client's input and still resolvable within 72 hours. Its source
label remains separate from its evidence reference for review and batch rollback.

The example `dream` grants are exactly `memory:read`, `memory:reflect` and
`memory:write`. They imply no domain read grants: its default input and writes
are limited to `general`. The owner must explicitly approve any broader domain
access; the runner follows the input's `allowed_domains` and never bypasses policy.
`SWITCHBOARD_SB`, `SWITCHBOARD_PYTHON` and `SWITCHBOARD_DELEGATE` overrides exist for
fixture tests and owner-managed wrappers; deployed examples use stable release paths.
