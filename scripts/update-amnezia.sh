#!/bin/sh
# Update the pinned AmneziaWG core (amneziawg-go + amneziawg-tools) of the Agent image.
#
#   scripts/update-amnezia.sh --check          exit 10 if newer releases exist, print them
#   scripts/update-amnezia.sh                  pin the latest releases (tags → exact commits)
#   scripts/update-amnezia.sh --go TAG --tools TAG
#   scripts/update-amnezia.sh --verify         also build and run unit tests + real-tunnel E2E
#
# Besides the pins it reports UAPI / config keys that appeared or disappeared between the old and new
# sources: those need a decision in packages/awg-config (adapter allowlist) and the client renderers.
# Host needs only git and Docker; the source diff runs in a throwaway container.
set -eu
cd "$(dirname "$0")/.."
DOCKERFILE=apps/node-agent/Dockerfile
GO_REPO=https://github.com/amnezia-vpn/amneziawg-go
TOOLS_REPO=https://github.com/amnezia-vpn/amneziawg-tools
CHECK=0; VERIFY=0; GO_TAG=''; TOOLS_TAG=''
while [ $# -gt 0 ]; do
    case $1 in
        --check) CHECK=1; shift ;;
        --verify) VERIFY=1; shift ;;
        --go) GO_TAG=$2; shift 2 ;;
        --tools) TOOLS_TAG=$2; shift 2 ;;
        *) sed -n '2,11p' "$0"; exit 2 ;;
    esac
done

pinned() { sed -n "s/^ARG $1=//p" "$DOCKERFILE" | head -1; }
latest_tag() { git ls-remote --tags --refs "$1" | sed 's|.*refs/tags/||' | grep -E '^v[0-9]+(\.[0-9]+)*$' | sort -V | tail -1; }
tag_commit() {  # annotated tags resolve through ^{} to the commit
    refs=$(git ls-remote --tags "$1" "refs/tags/$2" "refs/tags/$2^{}")
    echo "$refs" | awk '/\^\{\}$/ {print $1; found=1} END {if (!found) exit 1}' ||
        echo "$refs" | awk 'NR==1 {print $1}'
}

OLD_GO=$(pinned AWG_GO_VERSION); OLD_GO_COMMIT=$(pinned AWG_GO_COMMIT)
OLD_TOOLS=$(pinned AWG_TOOLS_VERSION); OLD_TOOLS_COMMIT=$(pinned AWG_TOOLS_COMMIT)
NEW_GO=${GO_TAG:-$(latest_tag "$GO_REPO")}
NEW_TOOLS=${TOOLS_TAG:-$(latest_tag "$TOOLS_REPO")}
[ -n "$NEW_GO" ] && [ -n "$NEW_TOOLS" ] || { echo 'Cannot read upstream tags (network?)' >&2; exit 1; }
NEW_GO_COMMIT=$(tag_commit "$GO_REPO" "$NEW_GO")
NEW_TOOLS_COMMIT=$(tag_commit "$TOOLS_REPO" "$NEW_TOOLS")

echo "amneziawg-go:    $OLD_GO ($OLD_GO_COMMIT) -> $NEW_GO ($NEW_GO_COMMIT)"
echo "amneziawg-tools: $OLD_TOOLS ($OLD_TOOLS_COMMIT) -> $NEW_TOOLS ($NEW_TOOLS_COMMIT)"
if [ "$OLD_GO_COMMIT" = "$NEW_GO_COMMIT" ] && [ "$OLD_TOOLS_COMMIT" = "$NEW_TOOLS_COMMIT" ]; then
    echo 'Already up to date.'
    exit 0
fi
[ "$CHECK" = 1 ] && exit 10

echo
echo '== Keys that changed upstream (adapter allowlist: packages/awg-config; renderers: packages/subscription-renderers)'
docker run --rm --network bridge golang:1.25.7-bookworm sh -c '
    keys_go() { git -C /go-src checkout -q "$1" && grep -hoE "case \"[a-z0-9_]+\"" /go-src/device/uapi.go | cut -d\" -f2 | sort -u; }
    keys_tools() { git -C /tools checkout -q "$1" && grep -hoE "key_match\(\"[A-Za-z0-9]+\"\)" /tools/src/config.c | cut -d\" -f2 | sort -u; }
    git clone -q '"$GO_REPO"' /go-src && git clone -q '"$TOOLS_REPO"' /tools
    keys_go '"$OLD_GO_COMMIT"' > /tmp/go-old; keys_go '"$NEW_GO_COMMIT"' > /tmp/go-new
    keys_tools '"$OLD_TOOLS_COMMIT"' > /tmp/tools-old; keys_tools '"$NEW_TOOLS_COMMIT"' > /tmp/tools-new
    echo "UAPI (amneziawg-go device/uapi.go):"; diff /tmp/go-old /tmp/go-new | sed -n "s/^[<>]/  &/p" || true
    echo "Config file (amneziawg-tools src/config.c):"; diff /tmp/tools-old /tmp/tools-new | sed -n "s/^[<>]/  &/p" || true
    echo "README headings mentioning AWG versions:"; grep -E "^#+ .*AWG [0-9]" /go-src/README.md | sed "s/^/  /" || true
' || echo '  (source diff unavailable; review the upstream changelog manually)'
echo '  ("> key" = new: decide whether the adapter should allow it; "< key" = removed: adapter must stop sending it)'

tmp=$(mktemp)
sed -e "s|^ARG AWG_GO_VERSION=.*|ARG AWG_GO_VERSION=$NEW_GO|" -e "s|^ARG AWG_GO_COMMIT=.*|ARG AWG_GO_COMMIT=$NEW_GO_COMMIT|" \
    -e "s|^ARG AWG_TOOLS_VERSION=.*|ARG AWG_TOOLS_VERSION=$NEW_TOOLS|" -e "s|^ARG AWG_TOOLS_COMMIT=.*|ARG AWG_TOOLS_COMMIT=$NEW_TOOLS_COMMIT|" \
    "$DOCKERFILE" > "$tmp" && cat "$tmp" > "$DOCKERFILE" && rm -f "$tmp"
doc=docs/agent.md  # links to the pinned sources
{
    tmp=$(mktemp)
    sed -e "s|amneziawg-go $OLD_GO\](https://github.com/amnezia-vpn/amneziawg-go/tree/$OLD_GO_COMMIT)|amneziawg-go $NEW_GO](https://github.com/amnezia-vpn/amneziawg-go/tree/$NEW_GO_COMMIT)|" \
        -e "s|amneziawg-tools $OLD_TOOLS\](https://github.com/amnezia-vpn/amneziawg-tools/tree/$OLD_TOOLS_COMMIT)|amneziawg-tools $NEW_TOOLS](https://github.com/amnezia-vpn/amneziawg-tools/tree/$NEW_TOOLS_COMMIT)|" \
        "$doc" > "$tmp" && cat "$tmp" > "$doc" && rm -f "$tmp"
}
echo
echo "Pinned in $DOCKERFILE."

if [ "$VERIFY" = 1 ]; then
    scripts/lab.sh build
    scripts/lab.sh test
    scripts/lab.sh e2e
    echo 'Core update verified: unit tests and the real AmneziaWG 3.1 tunnel E2E pass.'
else
    echo 'Next: scripts/update-amnezia.sh --verify  (or scripts/lab.sh build && scripts/lab.sh test && scripts/lab.sh e2e)'
fi
