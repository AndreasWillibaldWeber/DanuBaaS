#!/bin/sh
set -eu
MQTT_TELEGRAF_PASSWORD=$(cat /run/secrets/mqtt_telegraf_password)
PGPASSWORD=$(cat /run/secrets/db_telegraf_password)
export MQTT_TELEGRAF_PASSWORD PGPASSWORD
exec telegraf --config /etc/telegraf/telegraf.conf
