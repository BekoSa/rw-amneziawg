#!/bin/sh
# Installs the AWG Agent on a VPN server (next to, not inside, Remnawave Node).
# Needs the node key printed by the extension TUI (Ноды → n); it is asked interactively or read from
# --secret-file, never passed on the command line (argv leaks into ps, shell history and sudo logs). Only this project's directory and
# containers are created; AmneziaWG, its routes and nftables rules live inside the Agent container.
set -eu

SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
DIR=/opt/awg-node
SECRET_FILE=''
UDP_PORTS=51820
MANAGEMENT_PORT=8443
MANAGEMENT_BIND=0.0.0.0
REGISTRY=''
IMAGE_TAG=latest
UDP_BIND=0.0.0.0

while [ $# -gt 0 ]; do
    case $1 in
        --secret-file) SECRET_FILE=$2; shift 2 ;;
        --dir) DIR=$2; shift 2 ;;
        --udp-ports) UDP_PORTS=$2; shift 2 ;;              # e.g. 51820 or 51820-51822 (profile ports)
        --management-port) MANAGEMENT_PORT=$2; shift 2 ;;
        --management-bind) MANAGEMENT_BIND=$2; shift 2 ;;  # restrict to a private interface if you have one
        --udp-bind) UDP_BIND=$2; shift 2 ;;
        --image-registry) REGISTRY=${2%/}; shift 2 ;;  # CI images, e.g. ghcr.io/bekosa/rw-amneziawg
        --image-tag) IMAGE_TAG=$2; shift 2 ;;
        *) echo 'Usage: sudo ./install-node.sh [--secret-file FILE] [--udp-ports 51820] [--management-port 8443] [--management-bind IP]'; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die 'Docker is required'
docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is required'
[ -c /dev/net/tun ] || die '/dev/net/tun is missing on this server (enable TUN for the VPS in your provider panel)'

mkdir -p "$DIR/pki"
chmod 700 "$DIR"
# Traverse-only: the Agent (root without DAC override) must reach its key; the key itself stays 0600.
chmod 711 "$DIR/pki"
cp "$SOURCE_DIR/deploy/production/node.compose.yaml" "$DIR/compose.yaml"
{
    echo "AWG_SOURCE_DIR=$SOURCE_DIR"
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
    say "Pulling $AGENT_IMAGE"
    dc pull awg-agent
else
    say 'Building the Agent image (pinned amneziawg-go and amneziawg-tools, see apps/node-agent/Dockerfile)'
    dc build awg-agent
fi
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
say "Starting the Agent (node $NODE_ID)"
dc up -d --pull never
say 'Done'
printf '\n  Node %s is running. Management API: TCP %s (mutual TLS), AmneziaWG: UDP %s.\n' "$NODE_ID" "$MANAGEMENT_PORT" "$UDP_PORTS"
printf '  Docker-published ports bypass UFW. To allow management only from your panel server, e.g.:\n'
printf '    iptables -I DOCKER-USER -p tcp --dport 8443 ! -s <PANEL_IP> -j DROP\n'
printf '  or pass --management-bind <private IP>. Then press "Нода установлена — подключить" in the TUI.\n'
printf '  A profile must use one of the UDP ports above; to add ports re-run with --udp-ports.\n'
