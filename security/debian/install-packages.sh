#!/bin/sh
# Explicit host administration; never called by make dev or make prod.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
[ "$(id -u)" -eq 0 ] || { echo 'Run as root on the Debian server.' >&2; exit 1; }
. /etc/os-release
[ "$ID" = debian ] || { echo 'Debian is required.' >&2; exit 1; }
case "$VERSION_ID" in 12|13) ;; *) echo 'Review this baseline for your Debian release before installing.' >&2; exit 1;; esac
apt-get update
apt-get install --no-install-recommends nftables openssh-server sudo fail2ban python3-systemd \
    unattended-upgrades apparmor apparmor-utils needrestart ca-certificates
printf '%s\n' 'Packages installed. Review generated policies and follow README.md; no custom policy was applied.'
