#!/bin/sh
set -eu
CADDY_PASSWORD_HASH=$(cat /run/secrets/caddy_password_hash)
export CADDY_PASSWORD_HASH
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
