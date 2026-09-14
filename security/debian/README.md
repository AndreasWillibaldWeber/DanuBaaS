# Debian host security for DanuBaaS

This directory provides a reviewed baseline for a **dedicated Debian 12/13,
systemd, rootful Docker bridge-network server**. It contains an nftables policy
generator and trial/rollback installer, SSH and fail2ban policies, conservative
kernel settings, security-update configuration, and an audit script.

The host tools are deliberately separate from `make dev` and `make prod`: those
commands deploy the application; they must not reconfigure the developer's or
operator's host firewall. No host configuration is changed by cloning this folder
or rendering its templates. Do not treat a successful installation as a security
certification. Adapt the policy for the actual network and test from outside it.

## Files

| File | Purpose |
| --- | --- |
| `host.example.json` | Explicit interface, administrator, MQTT-source and DHCP settings |
| `render.py` | Validate settings and render six configuration files, without root |
| `firewall.py` | Root-only five-minute firewall trial, confirmation and rollback |
| `verify-ssh.py` | Validate installed SSH syntax and effective per-user/client policy |
| `install-packages.sh` | Install Debian security packages explicitly |
| `audit.sh` | Read-only host inventory; missing tools/privileges are not proof of health |
| `tests/` | Non-root contracts and optional isolated network-policy tests |

Generated files belong in the following locations:

| Generated file | Destination |
| --- | --- |
| `firewall.nft` | `/etc/danubaas-security/firewall.nft`, installed by firewall confirmation |
| `danubaas-firewall.service` | `/etc/systemd/system/`, installed by firewall confirmation |
| `00-danubaas.conf` | `/etc/ssh/sshd_config.d/` |
| `danubaas.local` | `/etc/fail2ban/jail.d/` |
| `60-danubaas.conf` | `/etc/sysctl.d/` |
| `52danubaas-unattended-upgrades` | `/etc/apt/apt.conf.d/` |

## 1. Prepare the server and recovery access

Run commands below from the `DanuBaaS/` repository root on the intended server.
First arrange the provider's serial/web console or local console, take a VM
snapshot if available, and record the existing firewall/SSH policy. Retain a
working privileged session throughout access changes. Test a **new** public-key
login and `sudo -v` for each intended administrator before disabling password or
root login. Never paste private keys into the repository.

Use an individual non-root administration account, with an SSH key installed
through the provider or Debian's normal account-management tools. Prefer a
passphrase-protected or hardware-backed key. Membership of the `docker` group is
root-equivalent; grant it only to trusted host administrators. Applications do
not need the Docker socket mounted. Keep emergency access credentials separately.

```sh
sudo sh security/debian/install-packages.sh
sudo sh security/debian/audit.sh
ip -brief address
ip route show default
ip -6 route show default
```

Package installation uses the host's configured APT repositories. Package scripts
may start default services; review their state afterwards. This does not install
Docker or replace Debian's repository configuration. Confirm the release remains
supported and arrange regular release upgrades separately.

Copy `host.example.json` to a private directory outside the repository, e.g.
`$HOME/danubaas-host.json`, and edit it:

- Replace `ens3` with **all externally reachable interfaces**, including public,
  private LAN and administrative VPN interfaces. Additional UDP VPN listener ports
  are not opened by this baseline; provide a separately reviewed rule if needed.
- Replace documentation addresses with your actual trusted SSH client CIDRs.
  IPv4 and IPv6 are independent. Empty administrator lists and whole-Internet SSH
  ranges are rejected. Do not assume a dynamic residential address stays fixed.
- Set existing non-root `admin_users` and the existing SSH port. If changing the
  SSH port, coordinate the old and new firewall/listener rules manually from the
  console; the trial rejects an SSH session using a different port.
- Restrict `mqtt_networks` to gateway/VPN networks when stable addresses permit.
  The example allows Internet clients on MQTT TLS because LTE addresses may vary;
  broker credentials, ACLs and certificates remain mandatory.
- Enable `dhcp4`/`dhcp6` only when required by the provider's network configuration.
  IPv6 ICMP, neighbour discovery and PMTU traffic remain allowed.

Render to a new output directory; the renderer refuses to overwrite one:

```sh
python3 security/debian/render.py --config "$HOME/danubaas-host.json" \
  --output "$HOME/danubaas-host-policy"
```

Review the generated files. Keep the JSON and output private; neither contains
passwords, but they describe administrative addresses and accounts. Unknown keys,
repeated fields, malformed networks and injected configuration syntax are rejected.

## 2. Install and test the firewall

The policy permits TCP 80/443 for Caddy, TCP 8883 from configured MQTT sources,
and SSH only from configured administrator networks. It does not expose PostgreSQL,
Grafana's direct port, Node-RED's editor, the API or its health listener. Current
Compose publishes TCP 443 only; UDP 443/HTTP3 is not enabled by this policy.

Docker-published connections traverse forwarding after destination NAT. Therefore
this policy filters **both input and forwarding**, using the original published
TCP port. It replaces only `table inet danubaas_host` in an atomic nft transaction.
It never runs `flush ruleset` or edits Docker/fail2ban-owned tables. Existing
established connections are retained; use new connections when testing restrictions.

New forwarding is denied by default. Traffic arriving from Docker's standard
`docker0` and `br-*` bridge names proceeds to Docker's own isolation/NAT rules.
Container egress and bridge isolation stay under Docker's control. Custom bridge
names, rootless Docker, Swarm, Kubernetes, routers and non-DNAT/routed container
networks need a separate policy review. An allow verdict here does not override
another firewall's drop verdict.

Do not enable Debian's stock `nftables.service` alongside this installer: its
configuration or shutdown behavior may flush rules belonging to Docker. The
trial refuses active nftables/ufw/firewalld services. If one exists, reconcile and
migrate its rules **from the console** first; do not blindly stop an existing
firewall. Do not disable Docker's iptables/ip6tables rule management or change its
firewall backend to follow this guide. These additional nft hooks are intended
as restrictions alongside Docker's existing backend.

```sh
sudo --preserve-env=SSH_CONNECTION python3 security/debian/firewall.py try \
  --config "$HOME/danubaas-host.json" --console-access-confirmed
```

The installer verifies interfaces, rejects documentation-only SSH addresses,
checks the current SSH source/port when `SSH_CONNECTION` is available, runs
`nft --check`, snapshots the previous managed table, and arms a systemd rollback
**before** loading the policy. It copies recovery code into a root-owned private
`/run` directory; recovery does not depend on the checkout remaining available.

Within five minutes, from a separate terminal:

1. Open a **new** SSH connection from the intended administrator network.
2. Verify HTTPS and a real MQTT TLS connection using the configured certificates.
3. From another source, verify SSH is blocked. Check both IPv4 and IPv6 if routed.
4. Verify forbidden ports and direct container-IP access are blocked; confirm
   container DNS/egress and the application's write/read routes still work.

After those checks, confirm from the new administrator session:

```sh
sudo python3 security/debian/firewall.py confirm
```

Confirmation writes boot persistence and enables `danubaas-firewall.service`.
Previous persisted files are retained with `.previous` suffixes when replacing a
previous installation. Only confirmation changes boot configuration. Without
confirmation, the previous **runtime** table is restored after five minutes.
Do not reboot during the trial; the transient timer lives in `/run`, while the
previous boot configuration remains in effect.

To abandon a pending trial immediately:

```sh
sudo python3 security/debian/firewall.py rollback
```

After confirmation, modify the JSON and repeat the same trial/confirmation flow.
A rollback command with no pending trial does nothing. To remove an established
installation deliberately from a recovery console, disable its service and remove
only its table (`nft delete table inet danubaas_host`), after putting a replacement
policy in place. Never flush all tables. Deleting a table without disabling boot
persistence is not an uninstall.

After a planned reboot and Docker restart, repeat external probes. For resilience,
use the provider's firewall as a second boundary with the same intended ports.
A compromised root account can change the local firewall.

## 3. Install SSH hardening separately

Use a recovery console for this step and retain the existing privileged session.
The firewall's timed rollback covers **firewall rules only**, not SSH configuration.
Back up `/etc/ssh/sshd_config` and `/etc/ssh/sshd_config.d/` to a root-only directory
before editing. Record whether the target drop-in already existed so it can be
restored or removed accurately. Do not reboot until a new login has succeeded.

Debian's main configuration must include `/etc/ssh/sshd_config.d/*.conf` in the
global section. OpenSSH commonly uses the first obtained value; an earlier cloud
configuration or a `Match` block can change the effective policy. If `ssh.socket`
is active, review socket activation and `ListenStream` separately before changing
ports; this guide assumes the normal `ssh.service` listener.

```sh
sudo install -m 0644 "$HOME/danubaas-host-policy/00-danubaas.conf" \
  /etc/ssh/sshd_config.d/00-danubaas.conf
sudo /usr/sbin/sshd -t
# Use the real client address; add --client-hostname when Match Host is used.
sudo python3 security/debian/verify-ssh.py --config "$HOME/danubaas-host.json" \
  --client-address YOUR_ACTUAL_CLIENT_IP
```

If validation fails, restore/remove this drop-in **before any reload**. Inspect
`sshd -T -C user=operator,addr=CLIENT_IP,host=CLIENT_HOST` for conflicting policy.
After validation, run `sudo systemctl reload ssh.service`, then test a new
key-authenticated login and sudo access. If it fails, restore the previous files,
run `sshd -t`, and reload from the console or retained session.

The policy disables root/password/keyboard-interactive login, agent forwarding,
X11 and remote TCP forwarding. It retains only local TCP forwarding to
`127.0.0.1:1880` or `localhost:1880` for the Node-RED editor:

```sh
ssh -N -L 1880:127.0.0.1:1880 operator@SERVER
```

Open the editor at `http://127.0.0.1:1880/admin`; its application login remains
required. This is a public-key-only baseline, not a two-factor setup. Design PAM
or hardware-key requirements explicitly if organizational policy requires MFA.
Use Debian/OpenSSH's maintained cryptographic defaults rather than copying a
static list of algorithms from an old hardening guide.

## 4. Fail2ban, kernel settings and security updates

Back up any same-named files before installing these reviewed drop-ins:

```sh
sudo install -m 0644 "$HOME/danubaas-host-policy/danubaas.local" /etc/fail2ban/jail.d/danubaas.local
sudo fail2ban-client -t
sudo systemctl enable --now fail2ban.service
sudo systemctl restart fail2ban.service
sudo fail2ban-client status sshd

sudo install -m 0644 "$HOME/danubaas-host-policy/60-danubaas.conf" /etc/sysctl.d/60-danubaas.conf
sudo sysctl -p /etc/sysctl.d/60-danubaas.conf

sudo install -m 0644 "$HOME/danubaas-host-policy/52danubaas-unattended-upgrades" /etc/apt/apt.conf.d/52danubaas-unattended-upgrades
sudo unattended-upgrade --dry-run --debug
sudo systemctl enable --now apt-daily.timer apt-daily-upgrade.timer
sudo aa-status
```

Stop and restore the corresponding backup if validation fails. The SSH fail2ban
jail reads the systemd journal and uses its own nftables action/table. Trusted
administrator networks are exempt. It does not claim to protect Caddy, Grafana or
MQTT; proxy-aware application jails need separate log/address validation to avoid
banning the proxy instead of the attacker. Key-only access and source restriction
are the primary SSH controls, not fail2ban alone.

The sysctl policy avoids disabling IPv6, modifying Docker's forwarding flags, or
forcing reverse-path filtering that can break VPN/asymmetric routing. Record
previous runtime values before applying it: deleting the file alone does not undo
already applied sysctls. Other later drop-ins can override values at boot. Verify
the effective values after reboot. Review AppArmor status and Docker profiles;
do not disable AppArmor to work around a deployment failure.

The update policy selects Debian security origins and disables automatic reboot.
It does not promise zero downtime: package updates may restart services. Monitor
`/var/log/unattended-upgrades/`, `needrestart`, and reboot requirements. Schedule
kernel reboots, non-security updates, Debian release upgrades, Docker Engine
updates and container-image digest updates separately. The Compose images are
pinned; APT does not update those application images.

## 5. Operating checklist

- Keep only the necessary listeners. Run `audit.sh` and inspect `ss -lntup`, Docker
  publications, active firewall tables, SSH policy and failed services regularly.
- Probe TCP 22 (or the chosen SSH port), 80, 443, 8883, and blocked ports 1880,
  1883, 3000, 5432, 8080 and 8081 from a machine you control outside the server.
  Test IPv4 and IPv6 separately; local curls do not verify the ingress boundary.
- Review secrets, TLS expiry, administrator keys and account access. Follow
  [production configuration](../../deploy/PRODUCTION.md) for application secrets.
- Keep encrypted off-host backups and test restoration on a disposable host.
  Back up configuration/role definitions as well as data; record recovery steps
  without placing private keys or passwords in version control.
- Synchronize host time using the provider-approved Debian time service; verify
  `timedatectl`. Sensor timestamps and rate-based alerts depend on reliable clocks.
- Monitor filesystem capacity, update failures, authentication failures, service
  health and the alert notification backlog. Retain logs according to your policy;
  they contain network addresses and administration metadata.
- Record changes, owners and a patch/reboot schedule. Re-run access checks after
  changes to Docker, networking, firewall managers or SSH authentication.

## Tests and limitations

```sh
make test-security
# Optional: real nftables and DNAT tests, only in a new isolated network namespace.
sudo sh security/debian/tests/network_policy.sh
```

The unit tests never mutate host settings. The optional network test refuses the
host network namespace, creates disposable interfaces/processes inside its own
namespace tree, and tests nft rules plus representative destination NAT. It does
not replace a real Debian VM test with the pinned Docker stack. Before rollout,
verify the five-minute rollback, confirmation persistence, SSH drop-in precedence,
fail2ban bans, reboot behavior and actual Docker routing on a disposable Debian VM.

## Primary references

- [Docker firewall and nftables behavior](https://docs.docker.com/engine/network/firewall-nftables/): table ownership, chain priority and Docker forwarding.
- [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/): public versus loopback bindings.
- [Debian nftables guidance](https://wiki.debian.org/nftables): host firewall management.
- [Debian OpenSSH server manual](https://manpages.debian.org/trixie/openssh-server/sshd_config.5.en.html): Include/Match precedence and authentication/forwarding settings.
- [Debian security information](https://www.debian.org/security/): supported security maintenance and updates.
- [Debian automatic updates](https://wiki.debian.org/UnattendedUpgrades): update scheduling and configuration.
