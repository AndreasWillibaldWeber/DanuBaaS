#!/bin/sh
set -eu
umask 077
PGPASSWORD=$(cat /run/secrets/db_admin_password)
export PGPASSWORD
stamp=$(date -u +%Y%m%dT%H%M%SZ)
output="/backups/sensors-${stamp}.dump"
# Write to a temporary file; a failed dump never looks like a complete backup.
pg_dump --format=custom --no-owner --file="${output}.partial"
mv "${output}.partial" "$output"
printf 'Created %s\n' "$output"
