#!/bin/sh
# Read-only inventory. Does not claim compliance or scan remote hosts.
set -u
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
section() { printf '\n%s\n' "$1"; }
section 'OS and running kernel'
cat /etc/os-release
uname -r
section 'Listening sockets (process names require root)'
ss -lntup
section 'Interfaces and default routes'
ip -brief address
ip route show default
ip -6 route show default
section 'Firewall services'
systemctl --no-pager status danubaas-firewall nftables ufw firewalld 2>/dev/null || true
section 'DanuBaaS rules and Docker forwarding rules (root required)'
nft list table inet danubaas_host 2>/dev/null || true
iptables -S DOCKER-USER 2>/dev/null || true
ip6tables -S DOCKER-USER 2>/dev/null || true
section 'Effective global SSH policy (also check administrator-specific Match blocks)'
/usr/sbin/sshd -T 2>/dev/null | awk '/^(port|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|authenticationmethods|allowusers|allowtcpforwarding|permitopen|x11forwarding|allowagentforwarding) /'
section 'SSH socket activation'
systemctl --no-pager status ssh.socket 2>/dev/null || true
section 'Fail2ban SSH jail'
fail2ban-client status sshd 2>/dev/null || true
section 'Update timers and reboot requirement'
systemctl --no-pager list-timers 'apt-*' 2>/dev/null || true
if [ -f /var/run/reboot-required ]; then cat /var/run/reboot-required; fi
section 'AppArmor'
aa-status 2>/dev/null || true
section 'Docker published ports and privileged group membership'
docker ps --format 'table {{.Names}}\t{{.Ports}}' 2>/dev/null || true
getent group docker || true
section 'Follow README.md for external IPv4/IPv6 probes, backup restoration, and access verification.'
