# Roadmap

Phase 2 provides the skeleton and nine read capabilities, with persistent
approval/job machinery and three adapters. Phase 3 media/Home writes are implemented
and fake-backend tested. No real client registration, launchd installation,
secret provisioning, tunnel switch or backend mutation is part of this build.

## Phase 3: everyday writes

- [x] `media_request_movie` / `media_request_show` via Overseerr; preflight details,
  durable duplicate protection by kind/TMDB ID, scope and explicit TMDB allowlists.
  Search/disambiguation workflow documented; live writes remain owner verification.
- [x] `home_run_scene` / `home_set` through HomeRelay with separate control token,
  stable IDs, allow_all_safe plus deny lists; secure variants require explicit
  secure IDs and approval. Thermostats use safe control scope. Opaque scene contents
  cannot be classified; owner scene review and live write verification remain.
- [x] Native Swift 6 EventKit app, private same-UID socket, build/sign script and
  uninstalled GUI LaunchAgent template. Calendar/list discovery, 31-day native
  reads with IDs/due dates and truthful FrameServer fallback; allowlisted
  `calendar_create_event`, `reminders_add`, `reminders_complete`; local owner
  access CLI and doctor TCC reporting. Fake-socket and native validation tests.
- [ ] Owner deployment with stable signing identity, GUI LaunchAgent installation,
  TCC grants, policy allowlist selection and real-client write verification.
  Existing-event editing and deletion remain outside this phase.
- [x] Federate the seven existing `orb_*` tools and MCP event methods through
  Switchboard, with per-client token mappings, `orb:use`, action allowlists,
  private catalog caching, redacted audit and doctor diagnostics. Orb hub retains
  webhook delivery and subscription ownership; local tools survive hub outages.
- [x] Document the stable-release Dot tunnel switch, verification, plugin Refresh
  and rollback in [DOT.md](DOT.md). Fake-upstream HTTP/stdio tests cover both eras.
- [ ] Owner deploys the release, provisions/merges private config and policy,
  repoints the existing tunnel, refreshes ChatGPT and verifies real Orb events.
- [x] Things 3 task reads, project/area discovery, standard adds and single-to-do
  completion in the existing helper (bundle identity retained). Fixed compiled
  AppleScript handlers use descriptor parameters; task policy deny lists and
  current project/area checks gate writes. Separate disabled Things backend,
  local owner Automation access CLI, doctor, task skill and fake-socket/native tests.
- [ ] Native checklist and Evening adds: installed Things AppleScript dictionary
  exposes neither. These inputs fail `things_unsupported` before creation; a
  documented add-only URL bridge would require a separate implementation decision.
- [ ] Owner enables Things, merges task scopes/allowlists, rebuilds with the same
  signing identity, grants Automation and verifies real reads/writes. No live
  Things operation or access prompt was performed during development.
- Verify each real client registration, Dot's confirmation UI and Dot-started
  local Codex MCP visibility. Dedicated hub identity for Switchboard notifications.

## Owner context and weather

- [x] `context_get` derives a shared owner profile from existing read backends,
  system timezone, config and policy; per-section provenance/unavailability,
  five-minute discovery cache, live policy/defaults and SQLite aliases/preferences.
- [x] Read-only `context_resolve`: aliases, exact names, room/category groups and
  conservative fuzzy candidates; bounded results, ambiguity and discovery limits.
  Fresh alias target validation, shared storage caps, scopes and fake-backend tests.
  Preferences explicitly never change permissions, scopes, allowlists or approvals.
- [x] NWS home observations, daily/hourly forecasts and bounded active alerts;
  dedicated credential-free HTTPS client, fixed allowlisted origin, bounded redirects,
  timeout/body limits, per-endpoint caches and stale fallback with age. Fake NWS
  tests cover redirects, oversized responses, cache recovery and alert truncation.
- [ ] Owner merges intended private weather/context scopes and config and deploys
  through the reviewed release flow. No private config changes, deployment or live
  backend mutations were performed in the context/weather worktree.

## Phase 4: long jobs and events

- [x] `media_compress` (approval), `media_compress_status` and local
  `media_compress_list` implemented behind the disabled `backends.media_compress`
  flag against the media-ctrl owner's agreed `compress_request.py` contract.
  Fixed argv, scrubbed environment, bounded output/timeout, private durable receipts,
  per-client status/list ownership and policy library roots are fake-script tested.
  Doctor checks files only and never invokes admission. Managed lanes own eligibility,
  VMAF testing and installation; skipped with a reason is normal.
- [ ] Enable only after media-ctrl's `compress_request.py` merges. Owner reviews
  interpreter/script/state paths, `compress_roots` and private client scopes, then
  deploys a reviewed release and enables the backend in private config. No live
  admission or downstream lane/installer behavior has been verified by this build.
  The CLI is idempotent for open requests; status/list use its receipt IDs rather
  than Switchboard job handles. Cancellation and events remain future contracts.
- Resume-aware job handlers and upstream cancellation contracts, consistent
  interrupted-job recovery and progress. No current handler promises resume.
- Reuse `orb_mcp` events, signed webhook and outbox code as a shared package for
  Switchboard's own events (Orb's existing events are already federated);
  do not independently rewrite it. It was inspected for style, not ported in Phase 2.
- Add `approval.requested`, `media.available`, `compression.completed` and
  `download.stalled`, plus authenticated Orb answer resolution that verifies the
  originating grant and treats dismissal, expiry and silence as denial.

## Phase 5: watch capabilities and consolidation

- [x] Switchboard-owned `ops_health` probes replace the retiring :8050 aggregator:
  validated literal loopback/RFC1918 targets, credential-free concurrent GETs,
  3-second deadlines, 15-second single-flight cache, compact status/count/latency
  output and configured probe count in doctor. Legacy `backends.health` is accepted
  but unused. Cortex dashboard identity is limited to `ops:read` in example policy.
  Owner deployment and adding the probe list to private config remain outstanding.

- [x] Plex On Deck, recently added and bounded unwatched candidate pools; Tautulli
  multi-user history and privacy-filtered playback. All require `media:read`.
- [x] Explicit-request Plex watched/unwatched writes with numeric rating-key
  validation, `media:watch_state`, independent `allowlists.plex.rating_keys`,
  deny precedence, policy recheck and metadata readback. Concurrent duplicates
  are serialized and already-matching states do not scrobble again.
- [x] Read only the owner-published Media Doctor version-1 maintenance snapshot;
  compact queue/work-lane/install/attention fields, private-file validation,
  timestamp/age and 15-minute stale reporting. No planner/internal access.
- [x] Port library-grounded recommendations and authoritative owner corrections
  into `skills/switchboard-watch`, with tool-only evidence and explicit writes.
  Fake Plex/Tautulli and private-snapshot tests cover contracts, policy and errors.
- [ ] Owner provisions Plex/Tautulli files, merges private config and the new scope
  for intended clients, selects exact Plex rating-key allowlists, then deploys
  through the reviewed release flow and verifies account identity and behavior.
  No live Plex/Tautulli calls or maintenance writes were made in this worktree.

- One skill source linked to all clients; remove duplicates only after review.
- Tagged-release deployment, CI and drift auditing through the deployment control plane.
- Migrate clients to shared capabilities after parity checks.
- Knowledge/recipes keep their own authorities; register centrally without copying logic.
- Finance/trading and generic exec/raw HTTP remain excluded permanently.


## Continuous monitoring and alerts

- [x] RFC1918 GET probes through the existing native helper; app name/bundle ID
  retained, Local Network usage description, closed native URL validation,
  credential/cache/cookie-free URLSession, no redirects or response bodies.
  Owner-only `sb net request-access`; permission/unavailable-helper results are
  `unknown`, preserving incident counters. Loopback remains direct.
- [x] Server lifecycle monitoring with fresh cache-updating runs; persistent
  thresholds, incidents, reminders and recovery duration. Unknown freezes counters;
  all-fail cycles with a loopback witness use one host incident. Per-probe opt-out.
- [x] Transactional SQLite outbox, incident/kind dedupe, restart persistence,
  bounded retry/backoff and 24-hour failure. HTTPS ntfy and verified STARTTLS SMTP
  async delivery with a cancellable 30-second total deadline; host allowlists, private file rechecks and redacted error codes.
  `ops_alerts_status`, owner example `ops:admin` for `ops_alerts_test`, doctor cycle
  age/channel checks, fake sockets/HTTPS/SMTP and native validation tests.
- [ ] Owner rebuilds native helper using the existing stable signing identity,
  grants Local Network permission, provisions 0600 channel files, enables monitoring
  and merges `ops:admin` for owner in private policy, then deploys through the
  release flow. Verify test notification delivery and LAN reachability afterward.
  Development used only fakes; no private configuration, live service mutations,
  commit or deployment occurred in this worktree. External dead-man monitoring of
  the Mac/service itself remains a separate operational concern.

## Tool contract standardization (2026-11-03)

- [x] Canonical underscore names across local adapters, catalog, audit, feedback
  and skills; dotted aliases retain policy and exact approval bindings, including
  grants persisted before migration. Scopes and Orb contracts stay unchanged.
- [x] Shared result envelope with existing status, normalized outcome, UTC as_of,
  source, list truncation/effective limits, and retryable structured errors.
  Closed advertised schemas cover initial approval replies and selected fields.
- [x] Intent-based descriptions and bounded external display text sanitization.
  Prompt-injection regressions prove media/calendar/task reads create no writes
  or approvals; Orb federation preserves its upstream text/contracts.
- [x] Fixed integer Plex year/season/episode handling and legacy initialize
  advertising unserved events.
- [x] Preserve Seerr percent-encoded queries and status 4/5/6/7 regressions,
  successful-result schema validation and catalog list_changed coverage.
- [ ] Orb card approval replies remains separate: provisioning
  a hub identity and a verified resolver require owner-controlled setup.


## Review fixes and planned maintenance (2026-11-03)

- [x] Compression authorizes the target before admission. For a TMDB ID, Switchboard
  reads the movie's library folder from media-api (read-only) and checks it against
  `compress_roots` before `admit` runs; a movie not in the library returns not_found
  without calling media-ctrl. The receipt path is re-checked afterwards.
- [x] Calendar/reminder writes use one resource-policy snapshot, subtract explicit
  denials and reject an empty effective list before calling the native helper.
- [x] Recovery supersedes pending outage/reminder rows atomically; host recovery and
  reminders have correct Up/Still down messages. Terminal outbox rows retain 14 days
  of dedupe history, pruned at most hourly; monitor state stays bounded by probe names.
- [x] Native requests have 10-second execution deadlines (180 seconds for access
  prompts), disconnect cancellation, single-completion guards and one admission
  release. EventKit reminder fetch tokens and URLSession work cancel; synchronous
  AppleScript waits also carry a 10-second Apple Event timeout. Uncancellable OS
  permission dialogs may remain visible after the request ends.
- [x] SMTP uses asyncio streams, mandatory verified STARTTLS and authenticated
  delivery under one 30-second deadline; timeout/cancellation closes the transport.
- [x] Undeterminable legacy JSON-RPC error IDs are null across HTTP and stdio;
  modern 2026-07-28 errors retain omission behavior.
- [x] Persisted `ops_alerts_silence` / `ops_alerts_unsilence` planned maintenance
  controls (`ops:silence`, example clients except Cortex), active-silence status and
  audit attribution. State tracking continues, expiry alerts fresh, and previously
  delivered incidents still recover. Private policy/config remain owner-controlled.
- [x] Regression tests use fake backends/servers and never mutate live services.


## PlayStation 5 (2026-11-06)

- [x] `console_status` (home:read), `console_wake` / `console_standby` (new
  `console:control`), served by the Pi agent's `ps5` verb; helper and Python validate
  the closed `ps5 status|wake|standby` grammar.
- [ ] Owner runs `scripts/owner-setup-ps5.sh`: Pi agent v3, playactor, PSN + Link Device
  pairing, `console:control` grants (incl. Cortex).
- [x] Owner ran the setup; paired and granted (2026-11-06).
- [x] PS5Upload engine (loopback :19113, `backends.ps5upload`): hardware details and the
  open title in `console_status`, `console_games`, `console_launch`, `console_close`.
  Fixed routes only; never its shell, files, payloads, cheats, fan, reboot or raw call.
  Launch takes CUSA/PPSA IDs only (best effort: homebrew can reuse one).
- [ ] The engine only runs while the PS5Upload desktop app is open; a login item (owner's
  call) keeps details available.
- [ ] Later: expose the PS5 to Apple Home (homebridge-playstation) if the owner wants Siri.

## Next directions (design: agent-capability-layer/NEXT-DIRECTIONS.md)

Order: A audit v2 → B events → C routines, with D shared memory and E per-client tool
profiles alongside; light evals (F) follow E.

- [x] **A. Audit v2 (2026-11-06).**
  - **Rows and context:** typed `audit_log` rows replace one-JSON-per-row `audit` (migrated once on start).
    `Execution` context carries trace/span (from `_meta.traceparent`), the `switchboard/session` and
    `switchboard/turn` hints, MCP clientInfo, origin and causation.
  - **Arguments:** short scalars are stored verbatim, long text as `{len, sha256}`, secret-shaped
    values redacted in any field (`secretscan.py`), and `Capability.audit_hash_fields` forces hashing.
  - **Integrity:** a SHA-256 hash chain, with `sb audit verify`, `/v1/audit/verify` and the doctor's
    `audit_chain` check.
  - **Housekeeping:** hourly maintenance writes daily anchors, rotates the JSONL daily, and after 180
    days rolls rows up and prunes them behind a prune anchor.
  - **Tools:** `audit_query`, `audit_trace` and `audit_digest`. `audit:read` covers own calls,
    `audit:read_all` every client; grants are in `scripts/owner-grant-agent-access.sh`.
  - **Wrappers:** `sb` forwards `SWITCHBOARD_SESSION`/`SWITCHBOARD_TURN`/`TRACEPARENT` as headers.
- [ ] Off-box anchoring of daily audit heads, e.g. to the Pi or Gitea. The chain only catches edits
  from someone who cannot rewrite the whole database.
- [x] C1 routines for the home (2026-11-06): YAML language, validator, hash-bound owner
  approval, executor with journal and restart resume, HomeRelay event stream with resume,
  device/held/reachability/time/every/sun/manual triggers. Plan:
  `docs/home-automation/PLAN.md` in the owner's workspace.
- [ ] B1 events (Switchboard's own event log for routines' `event:` triggers), C2 (`ask`
  on the Orb, `wait_for`, `classify`), D1 memory, E1 tool profiles: see the design doc.

## Phase 18: storage health

- [x] Opt-in `storage_status` / `storage_history` under `ops:read`, including cortex:
  physical NVMe/USB SMART with standby-safe six-hour cache, native mount discovery
  tied to physical-store APFS references, shared container fill, hourly SQLite
  history (35 days), seven-day least-squares forecast and monitor incidents/silences.
  Remote fill reuses snapshot; doctor, owner-only SMART enable script and fake fixtures.
- [ ] Owner reviews private config, enables storage/monitor channels and deploys the
  reviewed release. APFS backup roles remain unknown with physical-only diskutil
  discovery; legacy Time Machine directories are detected. No live drive mutation.
## Git workflows

- [x] `git_*` tools for Gitea repositories, PRs, exact-commit CI and redacted job
  logs; allowlisted PR creation/comments/merge, CI reruns and annotated release
  tags. CI gates and SHA pinning prevent unchecked merges/tags. Local status uses
  fixed read-only Git commands and realpath-constrained roots/worktrees.
- [ ] Owner provisions the disabled Gitea backend, local roots and write-repository
  allowlist, runs the idempotent agent-grant script, reviews and deploys. No live
  configuration, checkout mutation, Gitea write or deployment performed here.
