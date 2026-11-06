# Switchboard

One loopback Python service gives every agent the same typed local capabilities.
The core owns logic, identity, policy, approvals, jobs and audit. MCP, HTTP and
JSON CLI are thin adapters. Skills describe workflow, never grant permission.
Status and next steps: `docs/ROADMAP.md`.

If AGENTS.local.md exists, read it too; it holds this deployment's private notes.

## This is meant to keep evolving

Nothing here is set in stone. If you use Switchboard and something is wrong,
slow, awkward, missing or badly described, **improve it**. Good changes include:

- fixing bugs;
- sharpening tool descriptions;
- adding a missing capability;
- making outputs smaller;
- adding tests or doctor checks;
- improving skills or docs.

Also fix ops problems (crash loops, noisy logs, stale config) when you find them.

- **Can't fix it now?** Record it with `switchboard_report_issue` (any client,
  including Dot and OpenClaw), or `sb issues add`. Coding agents should check
  `sb issues list` when they start work here and close what they fix.
- **Shipping a change:** use a reviewed branch and passing CI. Deploy reviewed
  revisions only through `scripts/deploy.sh`; it health-checks and rolls back.
- **What still needs the owner:**
  - weakening a hard rule below;
  - widening a client's scopes;
  - adding a `secure_*` allowlist entry;
  - anything touching another project's ownership boundary.

  Propose those in the PR description, or with `report_issue`, and don't merge
  them yourself.
- **Keep this file true.** When you learn something that would have saved you
  time, add it to this file, the relevant skill, or `docs/ROADMAP.md`.

## Hard rules

- Python >=3.13; use `.venv` (Homebrew Python is `/opt/homebrew/bin/python3`).
  Runtime dependencies are aiohttp and PyYAML; dev tools pytest, pytest-asyncio, ruff.
- Bind **127.0.0.1 only**, port 18800 by default. Never bind 0.0.0.0.
- Never add generic shell/exec or raw HTTP passthrough tools.
- **No finance/trading capabilities ever**, including read-only finance status:
  All finance integrations are out of scope. `ops_health`
  uses a positive list of non-finance services and discards aggregator finance data.
- Never expose credentials, the Docker socket, device pairing, OTA or reboot.
- Never print or commit secrets. Config contains secret FILE PATHS only. Code
  reads them at runtime; owned regular 0600 files, no symlinks. Identity is derived
  from the bearer token, never from a body, query parameter or claimed client name.
- Enforce policy on EVERY call, including calls to tools hidden from discovery.
  Unknown/missing policy fails closed. Permission annotations are hints only.
- Every capability call is audited in SQLite and redacted JSONL. Do not log
  request headers, backend bodies, arbitrary exception text or credential values.
- Tests use fake aiohttp servers or fakes, never live services. Read-only live
  GETs may be used for contract discovery; never call a live mutating endpoint.
- Only `scripts/deploy.sh` changes what is running. Never point the LaunchAgent,
  an MCP registration or a skill symlink at a development checkout or worktree.
- Live config, policy and tokens are in `~/.config/switchboard/` (0700/0600) and
  belong to the owner. Change policy only as described above.

## Ownership

Use backend public interfaces and respect each backend's ownership boundary.
Agree interface changes with its maintainer before implementing them.

## Layout and changes

- `src/switchboard/`: core and typed registry, policy/auth, SQLite stores,
  backend clients, doctor; `domains/` has one declaration/handler module per domain.
- `protocol.py`, `server.py`, `stdio.py`, `cli.py`: adapters only.
- `tests/`: contract, security, state, HTTP and subprocess tests.
- `config/`: examples only; `skills/`: canonical workflow text;
  `docs/CAPABILITIES.md`: generated catalog; `docs/ROADMAP.md`: remaining phases.

To add a capability: declare its name/title/concise use description, closed input
schema, output schema, scope, tier, timeout, idempotency and backend in its domain;
write a handler entering through Core; add fake-backend and denial tests;
regenerate `docs/CAPABILITIES.md` with `.venv/bin/sb manifest --markdown` and
update the relevant skill. The manifest emits all metadata as JSON. Our schema
validator deliberately implements a small documented JSON Schema subset; new
keywords must be implemented/tested rather than silently ignored.

Approval tools must never bypass exact client/capability/canonical-args binding,
expiry or atomic single use. Consumption happens before execution, even if the
handler fails. `approvals_confirm` is a special approval-tier resolver with no
second grant: policy trust and matching creator are mandatory. Notification
answers alone grant nothing. Long work should use `core.jobs.submit`; handlers
must cooperate with cancellation. No current handler supports restart resume.

Run:

```sh
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/sb manifest --markdown > docs/CAPABILITIES.md
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## Operations

- Run `sb doctor` for health and `scripts/deploy.sh --status` for releases.
- Merging publishes nothing to the open-source mirror: a pushed `v*` tag does. Release
  merged user-visible work in the same task (privacy check, README features, annotated tag).
- Logs live in `~/Library/Logs/switchboard/` (0700).
- MCP registrations and skill symlinks must reference the deployed release under
  `~/.local/share/switchboard/current`, never a development checkout.
- Deployment settings belong in private `~/.config/switchboard/deploy.env` (0600).
  See docs/SETUP.md for launchd templates and stable native signing.

Policy, tokens and deployed binaries are owner-controlled trust roots.
Agents with filesystem access to them can bypass a capability server; routine
clients should receive only their own token and the public adapters.

Nothing personal belongs in this repository. Keep deployment notes in
`AGENTS.local.md`, and use `.oss-export-exclude` for private snapshot-only files.

- **README:** it is the front page, written for people, not agents. Lead with what it does (a picture, a "you say → what happens" table, the real integrations by name), keep it short, and fold setup and development detail into `<details>` or `docs/`. Add each user-visible feature to it in the same change.
