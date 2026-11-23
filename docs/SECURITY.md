# Security model


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

## Owner activity

`activity:read` exposes an owner timeline of every agent's successful writes,
owner answers and service incidents. Grant it only to the owner's own clients
(the example grants it to `cortex`). It does not grant `audit:read` or expose raw
audit arguments. `ops_pulse` and `ops_service_history` share `ops:read`; service
history includes restart attribution but no board work. No live policy is changed.

## Slate answers

`board:read` reads cards and asks; `board:write` creates and withdraws asks.
`board:answer` is a separate write scope intended only for the owner's own surface,
such as the Server app's `cortex` client. Never grant `board:answer` to an agent
client. The repository write allowlist still applies, and a client cannot answer
its own ask. No live policy is changed by declaring this scope.

An answer records the owner's decision on that question only. Only an attached
confirm action can change the narrow settings described below; other answers
change no permissions or allowlists. Answers never resolve an approval.
Approvals keep their separate exact-argument, expiring, single-use flow.
Ask and answer text is stored in SQLite and posted to the card as comments: never
ask for secrets, passwords, tokens or keys. Audit arguments hash entire `answer`,
`options` and `fields` objects/arrays as well as prompts, details and custom labels;
their nested text is not copied to SQLite audit arguments or JSONL in clear.

### Slate ask actions

Clients with `board:write` and the repository allowlist can propose a confirm
action. Only a different client with the owner's `board:answer` scope can trigger
it by answering yes. Switchboard generates the action summary and forces
`destructive: true`, which requests Face ID in the Server app. The server trusts
the answering client's authenticated scope; it does not independently verify Face ID.

`grant_scope` adds one registered capability scope to an existing policy client.
`approvals:*` and `board:answer` are never grantable. No action changes repository,
resource or secure allowlists, confirmation trust, tokens or arbitrary settings.
`add_ask_media_root` adds an existing resolved directory (maximum eight roots),
excluding `/`, home, private config/state/token trees, `.config`, `.ssh`, and
directories containing those trees. Validation runs at creation and application,
including symlink resolution; redirected approved paths fail closed.

Changes validate a same-directory staged file using the policy loader or board
config validator, preserve exact old bytes in `<file>.before-ask-<ask_id>` (0600),
then fsync and atomically replace the target with a 0600 file. Existing backups
are never overwritten. Already-applied actions succeed without another write.
The policy reloads on its next check; picture roots also update live config.
Action results are saved with the answer, audited, and included in the card
comment. Failures save a generic failure result while still recording the answer;
no and withdrawal apply nothing. A later card-comment failure does not undo a
saved answer or file change. Action arguments are hashed in audit logs.
