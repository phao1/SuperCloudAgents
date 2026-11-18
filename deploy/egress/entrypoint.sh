#!/bin/sh
set -eu
umask 077
mkdir -p /tmp/proxy-ca
cp /ca/ca.crt /ca/ca.key /tmp/proxy-ca/
printf '%s' "$SQUID_CONF" | sed 's@/ca/@/tmp/proxy-ca/@g' > /tmp/squid.conf
if [ ! -d /tmp/ssl_db ]; then
    /usr/lib/squid/security_file_certgen -c -s /tmp/ssl_db -M 4MB
fi
chown -R proxy:proxy /tmp/ssl_db /tmp/proxy-ca /tmp/squid.conf
exec runuser -u proxy -- squid -N -f /tmp/squid.conf
