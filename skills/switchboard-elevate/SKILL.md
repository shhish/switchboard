---
name: switchboard-elevate
description: Request owner-approved root commands on this Mac through the phone-signed native helper; inspect recipes, propose updates and wait for the result.
---

When work needs root, call `elevate_status` first. Prefer an existing recipe and
supply exactly its declared arguments. Use a custom argv request only when no
recipe fits; explain the reason plainly (1–300 characters). Never tell the owner
to run sudo in a terminal. Pairing/install is handled by the Server app's Mac
admin flow; report missing installation there.

Call `elevate_request {card?, recipe, args?, reason}` or
`elevate_request {card?, steps, timeout?, reason}`. Custom requests have 1–5 steps,
each an argv array beginning with an absolute executable, up to 64 strings of
4096 UTF-8 bytes each. Optional timeout is 5–7200 seconds (default 600). There is
no implicit shell. Omitted card uses the duplicate-guarded general-repo card
“Root commands on the Mac”. Give each request one clear purpose.

The result is a destructive confirm ask with a Switchboard-authored summary.
The owner approves in the Server app: Face ID signs its exact payload with a
paired Secure Enclave key. Agents cannot sign or fabricate approval. A declined
or unsigned answer runs nothing. Never substitute another execution path.

Wait with `board_ask_wait {ask_id, timeout_s?}` (default 120, maximum 280 seconds).
An answered ask with a running action still waits; timed_out means the wait
expired, so wait again or read the card later. Report the final action result,
exit code and useful output; do not call a running command complete. Switchboard
keeps an output tail and posts one completion receipt on the card. A restart
queries the helper by nonce and never resubmits commands.

If the same custom command comes up twice, propose a reusable recipe file with
`elevate_update {card?, what: "recipes", path, reason}`. The owner sees the recipe
IDs added/removed/changed, and signs the file hash. Use `what: "helper"` only for
a deliberate helper binary update. Only the native helper installs approved
bytes; do not edit its trust roots, live policy or live configuration yourself.

Scopes: `elevate:read` for status; `elevate:request` for requests/updates, plus
the configured repository write allowlist. `elevate:pair` belongs only to the
owner's Server app, never an agent. Pairing writes public pending-key state for
the first install; additional keys require an already-paired phone's approval.
