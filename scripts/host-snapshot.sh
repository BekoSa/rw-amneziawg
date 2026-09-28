#!/bin/sh
# Read-only host inspection. Never run this inside a namespace-entering container.
set -eu
out=${1:?snapshot output directory required}
command -v ip >/dev/null || { echo 'Host ip utility absent: cannot certify host integrity' >&2; exit 2; }
mkdir -p "$out"
ip -j -4 route show table all > "$out/routes4.json"
ip -j -6 route show table all > "$out/routes6.json"
ip -j -4 rule show > "$out/rules4.json"
ip -j -6 rule show > "$out/rules6.json"
ip -j -details link show > "$out/links.json"
ip -j address show > "$out/addresses.json"
# Read-only firewall visibility is optional; lack of permission is recorded explicitly.
if command -v nft >/dev/null && nft -j list ruleset > "$out/firewall.json" 2> "$out/firewall-error.txt"; then
    printf '%s\n' nft > "$out/firewall-status.txt"
else
    printf '%s\n' 'unavailable: firewall certification not possible with current read permissions' > "$out/firewall-status.txt"
fi
