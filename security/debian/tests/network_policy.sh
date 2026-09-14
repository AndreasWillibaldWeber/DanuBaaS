#!/bin/sh
# This wrapper always creates a NEW namespace before invoking the test body.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
[ "$(id -u)" -eq 0 ] || { echo 'Run with sudo; all networking changes occur in a new namespace.' >&2; exit 1; }
DANUBAAS_PARENT_NETNS=$(readlink /proc/self/ns/net)
export DANUBAAS_PARENT_NETNS
test_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec unshare --net python3 "$test_directory/network_policy.py"
