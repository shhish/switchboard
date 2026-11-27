# Secret hand-over for Slate asks

Agents often need a key from the owner (an API key for a new integration, a rotated
token). A `text` ask can't carry one: answers are stored in SQLite, posted as card
comments, shown in the app's history and fingerprinted in the audit log. A `secret`
ask is a write-only channel. The value goes from the owner's answer straight into an
owner-allowlisted store on the Mac and nothing else keeps it. The ask records only
that a value was provided.

## Flow

1. **Allowlist.** The owner says which secrets agents may ask for. Each destination
   in `~/.config/switchboard/handover.yaml` (0600) has a name, a label written for
   the owner, and a store:
   - `keychain`: a login Keychain generic password whose service starts with
     `homelab.` (the convention existing consumers already read, e.g. the Plex
     curator's `homelab.tmdb`). Values are printable ASCII of at most 128 bytes,
     because `security` reads the value from a prompt line of that size;
   - `file`: `~/.config/switchboard/secrets/handover/<name>`, a 0600 regular file in a
     0700 directory, apart from Switchboard's own backend secrets.

   Optional `charset` (`hex`, `alnum`, `base64url`, `printable`), `min_length` and
   `max_length` catch a wrong paste before anything is written.
   An agent can propose a new destination with a confirm ask carrying the
   `add_secret_destination` action. The owner's Yes applies it: Switchboard writes the
   summary ("Lets agents ask you for TMDB API key, saved to Keychain item
   homelab.tmdb"), never the agent. The file is re-read on every ask and answer, so
   removing an entry takes effect at once and an open ask for it fails closed.
2. **Ask.** `board_ask kind=secret destination=<name>`. The ask carries the
   server-authored label, the store, and whether a value already exists (so the owner
   sees "replaces the saved key"). The card comment says where the value will be
   saved and that it never appears on the board.
3. **Answer.** The owner's surface sends `board_answer {"secret": "<value>"}` once, or
   `{"decline": true}`. Switchboard checks the destination is still allowlisted,
   validates the value, writes it, verifies it, and only then records the answer as
   `{"provided": true}` and comments "Provided". A failed write leaves the ask open.
4. **Use.** The consumer reads its store directly (`security find-generic-password -s
   homelab.tmdb -w`, or the file). Switchboard has no tool that returns a stored value:
   the channel is write-only.

## Threat model

What it protects against:

| Threat | Mitigation |
|---|---|
| Value lands in durable shared records (Gitea comments, SQLite asks, audit, JSONL, activity feed, memory) | The value is removed from the answer before any persistence; stored answer is `{"provided": true}`; audit redacts secret-named keys before hashing a field, so the value is not even fingerprinted |
| Value returned to agents or other clients | Output schemas for asks have no `secret` key, so a regression fails schema validation instead of leaking; no read tool exists |
| Value in error text or logs | Every failure is a fixed message raised `from None`; the HTTP server has no access log; the value is never on a command line (Keychain writes pass it on stdin) |
| Agent tricks the owner into pasting an unrelated credential | Destinations are owner-approved; the prompt the owner acts on names Switchboard's label and store, not agent text alone |
| Agent overwrites an important file or Keychain item | Files only under the hand-over directory; Keychain services only under `homelab.`; names are `[a-z0-9_-]`; symlinks, non-regular files and foreign owners are refused |
| Partial or wrong write | Temp file + fsync + atomic rename; every directory is opened by descriptor with `O_NOFOLLOW` and checked with `fstat`. Keychain writes are read back as exact bytes (the `-g` line, since `-w` prints some values as hex) and compared in constant time, because `security` exits 0 even when its double prompt mismatches |
| Failed Keychain write damages the existing key | Every read and delete names the login Keychain, which must also be the default, and the write is refused when another keychain on the search list holds the item (`add -U` would update that one); an unknown state (read error, missing keychain) stops before any change; an old value that couldn't be written back byte for byte is never replaced; a failed write restores the old value or removes the new item and verifies that, or says the item could not be put back |
| A dropped or timed-out request lets a second answer race the first write | A secret answer runs to completion even when its request is cancelled: the write keeps the board lock, then the receipt, comment and column restore all land |
| A FIFO or other special file in the allowlist's place | Opened with `O_NOFOLLOW | O_NONBLOCK` and checked with `fstat` before reading |
| Owner edits the allowlist while a destination is being added | The new file is built from the same bytes that are checked to be unchanged just before the atomic replace; otherwise the action fails and the owner's edit stands |
| Replay or second answer | Asks are single-answer under the board write lock; the asker can't answer its own ask; `board:answer` is required |
| Stale ask after the owner revokes a destination | The allowlist is read once at answer time, through a checked descriptor; that one snapshot is compared with the ask and is the one written to |

Accepted residual risks:

- **Same-user processes.** Anything running as the owner's macOS user (coding agents
  with a shell, the consumers themselves) can read the Keychain item and the file.
  The hand-over keeps keys out of shared records and out of tool-only clients
  (MCP-only agents, Orb, remote clients); it is not a vault against local code.
- **Transport.** The answer travels app → Cortex → Switchboard over the same TLS route
  as every other owner answer, so Cloudflare's edge and the Cortex process see it in
  plaintext in memory. Neither logs request bodies. End-to-end encryption to a per-ask
  key would remove this; it needs a crypto dependency or the signed native helper and
  is a possible later step.
- **Process memory.** Python strings can't be wiped; the value lives only in locals for
  the duration of the call.
- **Keychain rollback is best effort.** The `security` CLI has no atomic update of exact
  bytes. If writes keep failing so that even the restore doesn't verify, the old value
  may be lost; the answer then says the item could not be put back. A Security-framework
  write through the signed native helper would remove this.
- **Concurrent hand edits.** An `add_secret_destination` Yes refuses to run if the
  allowlist changed since it was read, but an owner edit landing during the final backup
  and rename (milliseconds) can still be overwritten; the `.before-ask-<id>` backup keeps
  the prior file. Same-user processes racing the Keychain checks fall under the first
  risk above.

## Rotation and revocation

- **Rotate:** ask again for the same destination; the new value replaces the old one
  atomically and the ask says it replaces a saved value. No backup copy is kept.
- **Revoke:** revoke the key at the provider, then remove the destination from
  `handover.yaml` (new asks and open asks fail closed) and delete the stored value
  (`security delete-generic-password -s homelab.<name>`, or remove the file).

## What the owner's surface must do

The Server app shows the label and store, takes the value in a secure field that is
never saved as a draft, logged or kept after sending, and sends it once. Cortex passes
`secret` through like other answer keys, checking shape only.
