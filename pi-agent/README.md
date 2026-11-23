# Switchboard Pi agent

Agent VERSION **5** adds AdGuard Home query logs and rule management.

`switchboard-pi-agent` is the only thing Switchboard's SSH key can run on the
Raspberry Pi. The key is in `~/.config/switchboard/secrets/remotehost-agent` on the Mac;
on the Pi its `authorized_keys` line carries
`restrict,from="<allowed-source-CIDRs>",command="/usr/local/bin/switchboard-pi-agent"`,
so there's no shell, forwarding or PTY, and no other command.

- **Contract:** the docstring at the top of the script. Switchboard's side is
  `src/switchboard/remote.py`, and `hostcmd.valid_request` lists the allowed requests.
  Change both together.
- **Install or update** (from the Mac):

  ```sh
  scp pi-agent/switchboard-pi-agent remotehost:/tmp/sbpa
  ssh remotehost 'sudo install -o root -g root -m 0755 /tmp/sbpa /usr/local/bin/switchboard-pi-agent && rm /tmp/sbpa'
  ```

- **Try it without the key:**

  ```sh
  ssh remotehost "SSH_ORIGINAL_COMMAND='checks' /usr/local/bin/switchboard-pi-agent"
  ```

- **First-time access:** the owner provisions the forced-command key and pinned
  host key in private config. Replace `<allowed-source-CIDRs>` with the specific
  source networks permitted for this deployment.
- **Rules:**
  - Restarts are limited to homebridge, adguard, nginx and kuma.
  - `refresh-updates` runs `apt-get update` only.
  - Never add reboot, package installs or arbitrary commands. Switchboard's hard rules exclude reboot.
- **AdGuard Home:** `dns-stats` reads `/control/status`, `/control/stats` and
  `/control/clients` on loopback; `dns-pause <1-120>` and `dns-resume` POST only
  `/control/protection`. The login lives in `/opt/switchboard-pi-agent/adguard.json`
  (0600, the agent's user). The owner's `scripts/owner-setup-adguard.sh` installs the
  agent and sets that file up, reusing the Homebridge AdGuard plugin's login if present.
- **AdGuard rules and queries** (VERSION 5, installation unchanged):
  - `dns-querylog <limit 1-200> <all|blocked> [search]` reads `/control/querylog`.
    Search accepts only ASCII domain/IP characters (`A-Z`, `a-z`, digits, `.`, `:`, `-`),
    up to 100 characters. Returns time, client IP/name, question, answer status/reason
    and matched rule text/filter IDs. Compatible with AdGuard Home v1.0.0 and v0.107.x:
    no server-side status parameter is sent. The blocked filter includes `Filtered*`
    reasons; `NotFiltered*` and `Rewrite*` are excluded. Fetches at most five pages
    (200 entries per blocked-filter page) using `older_than`, stopping at the requested
    match count or the end of the log. Missing/repeated cursors and scan/output caps
    mark the result `truncated`. Rule data comes from `rules[]`, with `rule`/`filterId`
    as a legacy fallback; an empty modern array takes precedence. No answer bodies
    are exposed. Five page requests may take up to 40 seconds.
  - `dns-rules` reads `/control/filtering/status`: filtering enabled, user rules,
    filter IDs/names, URL hosts only, enabled state, rule counts and update times.
    Read results are bounded and indicate truncation; user-rule text is capped at
    512 characters, 1000 user rules and 100 filter lists.
  - `dns-rule <allow|block|remove> <domain>` changes DNS for the whole house.
    Hostname labels are 1-63 ASCII letters/digits/hyphens, without leading/trailing
    hyphens; total length is at most 253, with an optional leading `*.`.
    Allow uses `@@||<domain>^`; block uses `||<domain>^`. Each replaces opposite
    user rules for exactly that target. Remove deletes exact hostname targets in
    Adblock, plain-domain and single-host hosts-file rules, preserving comments,
    regexes, unrelated domains and distinct wildcard targets. Repeated edits are
    idempotent. The full untruncated rule list is read before posting to
    `/control/filtering/set_rules`; malformed lists are refused.
  - `dns-filters-refresh` POSTs `{"whitelist": false}` to `/control/filtering/refresh`
    and returns the updated-list count. It may take up to 60 seconds.
  - These API calls stay on loopback, disable proxies and refuse redirects.
- **PS5:** `ps5 status` uses Sony's discovery protocol (UDP 9302, stdlib only, no
  credentials). `ps5 wake|standby` run playactor 0.4.1 from `/opt/switchboard-ps5`
  (manifest in `pi-agent/ps5/`) with the pairing in `/opt/switchboard-ps5/credentials.json`
  (0600). Install and pairing are the owner's `scripts/owner-setup-ps5.sh`; pairing is
  never exposed as a request.
