#!/bin/sh
set -eu
# Seed once. Edits made through the authenticated editor survive recreation in /data.
if [ ! -f /data/flows.json ]; then
    cp /opt/dbe/flows.json /data/flows.json
    cp /opt/dbe/flows_cred.json /data/flows_cred.json
fi
exec npm start -- --userDir /data --settings /opt/dbe/settings.js
