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
