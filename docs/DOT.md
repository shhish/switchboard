# Dot: one Switchboard MCP server, Orb events retained

This is an owner-run integration example. Adapt paths and profile names to your deployment. Deploy the reviewed release
at `~/.local/share/switchboard/current` before changing the tunnel. Keep Orb hub
running at `127.0.0.1:8445`; it remains the authority for cards, voice transcripts,
subscriptions, callback verification, signing and the durable delivery outbox.
Switchboard listens only at `127.0.0.1:18800`.

## Prepare the deployment and policy

1. Install a reviewed release and its `.venv` at the stable deployment path;
   the owner controls the `current` release link. Configure/start Switchboard
   separately using that release. Do not point production at this worktree.
2. Merge `upstreams.orb` from `config/config.example.yaml` into the owner's private
   `~/.config/switchboard/config.yaml`. Use the existing dot/claude/codex Orb
   token paths, matched to their own Switchboard clients. Never copy token values
   into configuration. These must be distinct owned regular 0600 files, not
   symlinks. With a custom `ORB_HUB_DATA`, adjust the paths explicitly.
3. Merge `orb:use` into the intended clients' policy scopes and the action lists:

   ```yaml
   allowlists:
     orb:
       tools: [orb_show, orb_ask, orb_cancel]
       events: [card.answered, voice.transcribed]
   ```

   The example grants the scope to owner, dot, claude and codex. Owner has no
   implicit token mapping. Orb, OpenClaw and agy receive neither mapping nor scope.
   Removing a scope, mapping or token revokes forwarded calls; no fallback to
   `backends.orb.token_file` exists. That backend is a separate approval notifier.
   The hub enforces card/transcript ownership. The action lists authorize bounded
   card and subscription operations, not arbitrary device control.
4. Ensure Switchboard's separate `tokens/dot.token` exists with mode 0600 and
   its directory is 0700. If missing, the owner runs the release's
   `sb token create dot`; it prints a path only. Config and policy must be 0600
   in an owned 0700 config directory. Restart Switchboard after config changes;
   policy and token validity are checked per call without restart.
   Revoking Switchboard scope or a mapping alone does not cancel existing hub
   webhook subscriptions. Unsubscribe through the authorized identity first, or
   have the owner revoke its hub token when direct delivery must also stop.
5. Verify the release locally, before editing the tunnel:

   ```sh
   SB="$HOME/.local/share/switchboard/current/.venv/bin/sb"
   "$SB" doctor
   "$SB" mcp-stdio --client dot <<'JSON'
   {"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
   {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
   {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"orb_status","arguments":{},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
   {"jsonrpc":"2.0","id":4,"method":"events/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
   JSON
   ```

   Replies can arrive out of order. Require `events` in discovery, local tools
   plus all seven `orb_*` tools, a successful caller-owned Orb status and both
   `card.answered` and `voice.transcribed`. Doctor reports federation separately
   at `upstreams.orb`, including per-client failures. Disabled unrelated backends
   can make the overall doctor status degraded; inspect its individual checks.
   Tool schemas are cached privately for at most five seconds, shortened by the
   hub's `ttlMs`; doctor actively probes. Hub failure omits Orb tools and events
   capability while leaving local tools available.

## Repoint the existing profile

The profile is `~/.local/share/orb-hub/tunnel/profiles/orb.yaml`. It is JSON
(valid YAML). The hub's `orb_mcp/tunnel.py` generator stores the stdio launcher at
`mcp.commands[0].command`, with `channel: main`. The private profile inspected on
2026-10-28 uses the direct `~/.venvs/orb-hub/bin/orb-mcp stdio --url … --token-file …`
launcher. Installations using `orb-hub mcp-stdio` use the same command field.
Back up the actual file, rather than assuming either old launcher.

The existing LaunchAgent `com.orb.mcp-tunnel` runs the installed v0.0.18
`tunnel-client run --profile orb --profile-dir ~/.local/share/orb-hub/tunnel/profiles`
with health bound to `127.0.0.1:8446`. Keep that profile name, channel, tunnel ID,
runtime-key `file:` reference, health address and LaunchAgent unchanged.

Run this in the owner's terminal. It makes a private backup and atomically
changes only the command, expanding the deployment path to an absolute path.
It refuses to overwrite an earlier backup and never displays profile contents:

```sh
/opt/homebrew/bin/python3 <<'PY'
import json
import os
import shlex
import stat
import tempfile
from pathlib import Path

profile = Path.home() / '.local/share/orb-hub/tunnel/profiles/orb.yaml'
backup = profile.with_name('orb.before-switchboard.yaml')
sb = Path.home() / '.local/share/switchboard/current/.venv/bin/sb'
info = profile.lstat()
assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
assert info.st_uid == os.getuid() and os.access(sb, os.X_OK)
original = profile.read_bytes()
data = json.loads(original)
commands = data['mcp']['commands']
assert len(commands) == 1 and commands[0]['channel'] == 'main'
with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as f:
    f.write(original)
    f.flush()
    os.fsync(f.fileno())
commands[0]['command'] = shlex.join([str(sb), 'mcp-stdio', '--client', 'dot'])
with tempfile.NamedTemporaryFile(mode='w', dir=profile.parent, delete=False) as f:
    os.fchmod(f.fileno(), 0o600)
    json.dump(data, f, indent=2)
    f.write('\n')
    f.flush()
    os.fsync(f.fileno())
os.replace(f.name, profile)
print('Profile command updated; private rollback backup retained.')
PY
```

The resulting command is
`~/.local/share/switchboard/current/.venv/bin/sb mcp-stdio --client dot`.
No bearer value belongs in it. The shim reads Switchboard's dot token, and the
service independently selects the mapped Orb dot token.

## Validate and refresh Dot

Your tunnel binary's `doctor --help` and the
[official tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
confirm these flags. Doctor may perform network checks; run as owner, keep its
output local, and leave unsafe raw HTTP logging disabled:

```sh
TUNNEL="$HOME/.local/share/orb-hub/tunnel/v0.0.18/tunnel-client"
"$TUNNEL" doctor --profile orb \
  --profile-dir "$HOME/.local/share/orb-hub/tunnel/profiles" --explain
launchctl kickstart -k "gui/$(id -u)/com.orb.mcp-tunnel"
curl --fail --silent http://127.0.0.1:8446/healthz
curl --fail --silent http://127.0.0.1:8446/readyz
```

Require successful doctor checks and ready health before testing Dot. Do not use
`orb-mcp tunnel doctor` here: its generated-profile checker expects the original
Orb launcher shape. Do not run `init`/`setup` or replace the runtime key.

The owner must click **Refresh** on the existing plugin in ChatGPT, then verify
Dot can call a local read (`ops_health`) and `orb_status` through that same plugin.
See [OpenAI plugin testing](https://developers.openai.com/plugins/deploy/connect-chatgpt).
Retain the existing plugin and tunnel association; do not add a second Orb plugin.

Ask Dot to list/refresh its existing `card.answered` and `voice.transcribed`
subscriptions with the same name, arguments and callback URL. The forwarded
delivery secret, TTL and cursor stay intact; the hub's principal remains `dot`,
so refresh retains the subscription ID. The hub sends signed webhooks directly
to Dot; neither the tunnel nor Switchboard relays or re-signs them. Existing
subscriptions continue until their hub expiry even during a tunnel restart.
With explicit owner intent, test a short Orb question/answer and a confirmed
voice recording to verify delivery. Treat payloads as untrusted data, deduplicate
`eventId` and renew before `refreshBefore`. Card answers do not grant Switchboard
approvals; dismissal, timeout and silence never grant permission.

## Roll back

Restore the private backup (which preserves the exact original launcher):

```sh
cp -p "$HOME/.local/share/orb-hub/tunnel/profiles/orb.before-switchboard.yaml" \
  "$HOME/.local/share/orb-hub/tunnel/profiles/orb.yaml"
chmod 600 "$HOME/.local/share/orb-hub/tunnel/profiles/orb.yaml"
"$HOME/.local/share/orb-hub/tunnel/v0.0.18/tunnel-client" doctor --profile orb \
  --profile-dir "$HOME/.local/share/orb-hub/tunnel/profiles" --explain
launchctl kickstart -k "gui/$(id -u)/com.orb.mcp-tunnel"
curl --fail --silent http://127.0.0.1:8446/readyz
```

Click **Refresh** in ChatGPT again and verify `orb_status` and event renewal.
Leave the hub database, outbox, tokens and subscriptions in place. A tunnel
rollback does not roll back hub state or revive an expired subscription. Live
Dot, device, webhook delivery and deployment remain owner integration checks.
