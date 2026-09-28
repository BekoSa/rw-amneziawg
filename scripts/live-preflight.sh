#!/bin/sh
set -eu
if [ ! -c /dev/net/tun ]; then
    echo 'BLOCKED: /dev/net/tun is absent. Use an isolated disposable runner with TUN already provisioned. No host changes were made.' >&2
    exit 78
fi
printf '%s\n' 'TUN prerequisite present; Docker device access still must pass runtime checks.'
