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

An answer records the owner's decision on that question only. It grants no
Switchboard permission, never widens scopes or allowlists, and never resolves an
approval. Approvals keep their separate exact-argument, expiring, single-use flow.
Ask and answer text is stored in SQLite and posted to the card as comments: never
ask for secrets, passwords, tokens or keys. Audit arguments hash entire `answer`,
`options` and `fields` objects/arrays as well as prompts, details and custom labels;
their nested text is not copied to SQLite audit arguments or JSONL in clear.
