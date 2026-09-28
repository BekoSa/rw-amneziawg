#!/bin/sh
# Installs the AWG Agent on a VPN server (next to, not inside, Remnawave Node).
# Needs the node key printed by the extension TUI (Ноды → n); it is asked interactively or read from
# --secret-file, never passed on the command line (argv leaks into ps, shell history and sudo logs). Only this project's directory and
# containers are created; AmneziaWG, its routes and nftables rules live inside the Agent container.
set -eu

SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
DIR=/opt/awg-node
SECRET_FILE=''
UDP_PORTS=''
MANAGEMENT_PORT=8443
MANAGEMENT_BIND=0.0.0.0
REGISTRY=''
REF=main
NO_PULL=0
DEFAULT_REGISTRY=ghcr.io/bekosa/rw-amneziawg
IMAGE_TAG=latest
UDP_BIND=0.0.0.0

while [ $# -gt 0 ]; do
    case $1 in
        --secret-file) SECRET_FILE=$2; shift 2 ;;
        --dir) DIR=$2; shift 2 ;;
        --udp-ports) UDP_PORTS=$2; shift 2 ;;              # e.g. 41234 or 41234-41236; default: random free port
        --management-port) MANAGEMENT_PORT=$2; shift 2 ;;
        --management-bind) MANAGEMENT_BIND=$2; shift 2 ;;  # restrict to a private interface if you have one
        --udp-bind) UDP_BIND=$2; shift 2 ;;
        --image-registry) REGISTRY=${2%/}; shift 2 ;;  # CI images, e.g. ghcr.io/bekosa/rw-amneziawg
        --image-tag) IMAGE_TAG=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;              # git ref of fetched files (standalone mode)
        --no-pull) NO_PULL=1; shift ;;         # use an image already present locally
        *) echo 'Usage: sudo ./install-node.sh [--secret-file FILE] [--udp-ports PORT|FROM-TO] [--management-port 8443] [--management-bind IP]'; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# Standalone mode: the script alone is enough. Without a checkout next to it, the few files it needs are
# fetched from GitHub (pinned by --ref) and the images come from GHCR, so nothing is built on the server.
fetch() {  # fetch REPO_PATH DEST
    case $RAW_BASE in
        /*) cp "$RAW_BASE/$1" "$2" ;;  # tests: a local tree instead of GitHub
        *) if command -v curl >/dev/null; then curl -fsSL "$RAW_BASE/$1" -o "$2"
           else wget -qO "$2" "$RAW_BASE/$1"; fi ;;
    esac || die "Cannot download $1 from $RAW_BASE"
}

RAW_BASE=${AWG_RAW_BASE:-https://raw.githubusercontent.com/BekoSa/rw-amneziawg/$REF}
if [ -f "$SOURCE_DIR/deploy/production/node.compose.yaml" ] && [ -f "$SOURCE_DIR/apps/node-agent/Dockerfile" ]; then
    STANDALONE=0
else
    STANDALONE=1
    REGISTRY=${REGISTRY:-$DEFAULT_REGISTRY}
fi

command -v docker >/dev/null || die 'Docker is required'
docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is required'
[ -c /dev/net/tun ] || die '/dev/net/tun is missing on this server (enable TUN for the VPS in your provider panel)'

random_port() {  # a random free UDP port in 20000-59999: no well-known AWG/WireGuard port to fingerprint
    tries=0
    while [ "$tries" -lt 50 ]; do
        port=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 40000 + 20000 ))
        if ! command -v ss >/dev/null || [ -z "$(ss -Hlun "sport = :$port" 2>/dev/null)" ]; then
            echo "$port"; return
        fi
        tries=$((tries + 1))
    done
    die 'Could not find a free UDP port; pass --udp-ports'
}
if [ -z "$UDP_PORTS" ]; then
    # Re-runs (updates) keep the published port, so existing profiles and client configs keep working.
    UDP_PORTS=$(sed -n 's/^AWG_UDP_PORTS=//p' "$DIR/.env" 2>/dev/null | head -1)
    [ -n "$UDP_PORTS" ] || UDP_PORTS=$(random_port)
fi

mkdir -p "$DIR/pki"
chmod 700 "$DIR"
# Traverse-only: the Agent (root without DAC override) must reach its key; the key itself stays 0600.
chmod 711 "$DIR/pki"
if [ "$STANDALONE" = 1 ]; then
    say "Fetching the node compose file ($REF)"
    fetch deploy/production/node.compose.yaml "$DIR/compose.yaml"
    BUILD_DIR=$DIR  # never built in standalone mode
else
    cp "$SOURCE_DIR/deploy/production/node.compose.yaml" "$DIR/compose.yaml"
    BUILD_DIR=$SOURCE_DIR
fi
{
    echo "AWG_SOURCE_DIR=$BUILD_DIR"
    echo "AWG_UDP_PORTS=$UDP_PORTS"
    echo "AWG_UDP_BIND=$UDP_BIND"
    echo "AWG_MANAGEMENT_PORT=$MANAGEMENT_PORT"
    echo "AWG_MANAGEMENT_BIND=$MANAGEMENT_BIND"
    [ -z "${AWG_NODE_INTERNAL_NETWORK:-}" ] || echo "AWG_NODE_INTERNAL_NETWORK=$AWG_NODE_INTERNAL_NETWORK"
    [ -z "$REGISTRY" ] || echo "AWG_AGENT_IMAGE=$REGISTRY/agent:$IMAGE_TAG"
} > "$DIR/.env"
chmod 600 "$DIR/.env"
dc() { docker compose -f "$DIR/compose.yaml" --env-file "$DIR/.env" "$@"; }

AGENT_IMAGE=awg-agent:local
if [ -n "$REGISTRY" ]; then
    AGENT_IMAGE="$REGISTRY/agent:$IMAGE_TAG"
    if [ "$NO_PULL" = 1 ]; then
        docker image inspect "$AGENT_IMAGE" >/dev/null || die "Image $AGENT_IMAGE not found locally"
    else
        say "Pulling $AGENT_IMAGE"
        dc pull awg-agent
    fi
else
    say 'Building the Agent image (pinned amneziawg-go and amneziawg-tools, see apps/node-agent/Dockerfile)'
    dc build awg-agent
fi
if [ -z "$SECRET_FILE" ] && [ -f "$DIR/pki/agent.crt" ] && [ -f "$DIR/pki/node.env" ]; then
    # Re-run (update): keep the enrolled identity; the node key is needed only for the first install.
    NODE_ID=$(sed -n 's/^AWG_NODE_ID=//p' "$DIR/pki/node.env")
    say "Node $NODE_ID is already enrolled; updating in place"
else
    if [ -n "$SECRET_FILE" ]; then
        SECRET=$(cat "$SECRET_FILE")
    else
        printf 'Paste the node key from the extension TUI (awgnode1:...): '
        stty -echo 2>/dev/null || true; read -r SECRET; stty echo 2>/dev/null || true; echo
    fi
    say 'Unpacking and verifying the node key'
    NODE_ID=$(printf '%s' "$SECRET" | docker run --rm -i --network none -v "$DIR/pki:/pki" \
        --entrypoint python "$AGENT_IMAGE" -m awg_agent.enroll /pki) || die 'Invalid node key'
    unset SECRET
fi
say "Starting the Agent (node $NODE_ID)"
dc up -d --pull never --force-recreate
say 'Done'
printf '\n  Node %s is running. Management API: TCP %s (mutual TLS), AmneziaWG: UDP %s.\n' "$NODE_ID" "$MANAGEMENT_PORT" "$UDP_PORTS"
printf '  Docker-published ports bypass UFW. To allow management only from your panel server, e.g.:\n'
printf '    iptables -I DOCKER-USER -p tcp --dport 8443 ! -s <PANEL_IP> -j DROP\n'
printf '  or pass --management-bind <private IP>. Then press "Нода установлена — подключить" in the TUI.\n'
printf '  The TUI fills this UDP port into new profiles of this node; to open more ports re-run with --udp-ports.\n'
