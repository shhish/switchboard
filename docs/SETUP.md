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
