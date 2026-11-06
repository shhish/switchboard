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
