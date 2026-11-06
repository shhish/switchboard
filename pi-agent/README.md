# Switchboard Pi agent

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
- **PS5:** `ps5 status` uses Sony's discovery protocol (UDP 9302, stdlib only, no
  credentials). `ps5 wake|standby` run playactor 0.4.1 from `/opt/switchboard-ps5`
  (manifest in `pi-agent/ps5/`) with the pairing in `/opt/switchboard-ps5/credentials.json`
  (0600). Install and pairing are the owner's `scripts/owner-setup-ps5.sh`; pairing is
  never exposed as a request.
