#!/bin/sh
# Invoked by install-node.sh after its read-only prerequisite/argument checks.
set -eu
DIR=${AWG_INSTALL_DIR:?installer directory required}
SOURCE=${AWG_INSTALL_SOURCE:?installer source required}
RAW_BASE=${AWG_INSTALL_RAW_BASE:?installer download origin required}
REGISTRY=${AWG_INSTALL_REGISTRY:-}
TAG=${AWG_INSTALL_IMAGE_TAG:-latest}
NO_PULL=${AWG_INSTALL_NO_PULL:-0}
SECRET_FILE=${AWG_INSTALL_SECRET_FILE:-}
REENROLL=${AWG_INSTALL_REENROLL:-0}
MANAGEMENT_PORT=${AWG_INSTALL_MANAGEMENT_PORT:-}
MANAGEMENT_BIND=${AWG_INSTALL_MANAGEMENT_BIND:-}
UDP_BIND=${AWG_INSTALL_UDP_BIND:-}
NETWORK=awg-node_awg
network_created=0
temporary=$(mktemp -d)
backup=
changed=0
# Capture immutable images before any pull/build can move the old mutable tag.
previous_agent_image=$(docker inspect --format '{{.Image}}' awg-node-awg-agent-1 2>/dev/null || true)
previous_ingress_image=$(docker inspect --format '{{.Image}}' awg-node-node-ingress-1 2>/dev/null || true)
say() { printf '==> %s\n' "$*"; }
dc() { docker compose -f "$DIR/compose.yaml" --env-file "$DIR/.env" "$@"; }
old_dc() {
    if [ "$(sed -n 's/^NODE_MANAGED=//p' "$DIR/.env" | head -1)" != 1 ] &&
       [ "$(sed -n 's/^TT_ENABLED=//p' "$DIR/.env" | head -1)" = 1 ] && [ -f "$DIR/trusttunnel.compose.yaml" ]; then
        docker compose -f "$DIR/compose.yaml" -f "$DIR/trusttunnel.compose.yaml" --env-file "$DIR/.env" "$@"
    else dc "$@"; fi
}
finish() {
    status=$?
    trap - EXIT INT TERM
    if [ "$status" != 0 ] && [ "$changed" = 1 ]; then
        # Never remove volumes or replace PKI during configuration rollback.
        dc down >/dev/null 2>&1 || true
        for file in compose.yaml .env trusttunnel.compose.yaml; do
            if [ -f "$backup/$file" ]; then cp -p "$backup/$file" "$DIR/$file";
            else rm -f "$DIR/$file"; fi
        done
        if [ -f "$DIR/compose.yaml" ] && [ -f "$DIR/.env" ]; then
            say 'Restoring the previous node configuration'
            # Shell environment overrides Compose's restored env-file, without rewriting it.
            if [ -n "$previous_agent_image" ]; then export AWG_AGENT_IMAGE="$previous_agent_image"; fi
            if [ -n "$previous_ingress_image" ]; then export AWG_INGRESS_IMAGE="$previous_ingress_image"; fi
            old_dc up -d --pull never >/dev/null 2>&1 ||
                printf 'Previous configuration restored; container restart failed. Backup: %s\n' "$backup" >&2
        fi
    fi
    if [ "$status" != 0 ] && [ "$network_created" = 1 ]; then
        docker network rm "$NETWORK" >/dev/null 2>&1 || true
    fi
    rm -f "$DIR/.managed-compose-$$" "$DIR/.managed-env-$$"
    rm -rf "$temporary"
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
fetch() {
    if [ -f "$SOURCE/$1" ]; then cp "$SOURCE/$1" "$2"; return; fi
    case $RAW_BASE in
        /*) cp "$RAW_BASE/$1" "$2";;
        *) if command -v curl >/dev/null; then curl -fsSL "$RAW_BASE/$1" -o "$2";
           else wget -qO "$2" "$RAW_BASE/$1"; fi;;
    esac
}
fetch deploy/production/node.managed.compose.yaml "$temporary/compose.yaml"
fetch scripts/provision_managed_node.py "$temporary/provision_managed_node.py"
AGENT_IMAGE=awg-agent:local
INGRESS_IMAGE=awg-ingress:local
if [ -n "$REGISTRY" ]; then
    AGENT_IMAGE=$REGISTRY/agent:$TAG
    INGRESS_IMAGE=$REGISTRY/ingress:$TAG
    for image in "$AGENT_IMAGE" "$INGRESS_IMAGE"; do
        if [ "$NO_PULL" = 1 ]; then docker image inspect "$image" >/dev/null;
        else say "Pulling $image"; docker pull "$image"; fi
    done
else
    docker build -f "$SOURCE/apps/node-agent/Dockerfile" -t "$AGENT_IMAGE" "$SOURCE"
    docker build -f "$SOURCE/apps/node-ingress/Dockerfile" -t "$INGRESS_IMAGE" "$SOURCE"
fi
# An old image must fail before replacing any working Compose/env.
docker run --rm --network none --entrypoint python "$AGENT_IMAGE" -c \
    'from awg_contracts import NodeInstallEnvelope; from awg_agent.managed_node import NodeResourceService' >/dev/null
mkdir -p "$DIR/pki"
chmod 700 "$DIR"
chmod 711 "$DIR/pki"
if [ -z "$SECRET_FILE" ] && [ "$REENROLL" = 0 ] && [ -f "$DIR/pki/agent.crt" ] && [ -f "$DIR/pki/node.env" ]; then
    NODE_ID=$(sed -n 's/^AWG_NODE_ID=//p' "$DIR/pki/node.env")
else
    if [ -n "$SECRET_FILE" ]; then secret=$(cat "$SECRET_FILE");
    else
        printf 'Paste the node key from the extension TUI: '
        stty -echo 2>/dev/null || true
        read -r secret
        stty echo 2>/dev/null || true
        printf '\n'
    fi
    NODE_ID=$(printf '%s' "$secret" | docker run --rm -i --network none -v "$DIR/pki:/pki" \
        --entrypoint python "$AGENT_IMAGE" -m awg_agent.enroll /pki)
    unset secret
fi
if [ -z "$MANAGEMENT_PORT" ]; then
    MANAGEMENT_PORT=$(sed -n 's/^AWG_MANAGEMENT_PORT=//p' "$DIR/.env" 2>/dev/null | head -1)
fi
if [ -z "$MANAGEMENT_PORT" ]; then
    MANAGEMENT_PORT=$(sed -n 's/^AWG_NODE_MANAGEMENT_PORT=//p' "$DIR/pki/node.env")
fi
MANAGEMENT_PORT=${MANAGEMENT_PORT:-8443}
if [ -z "$MANAGEMENT_BIND" ]; then
    MANAGEMENT_BIND=$(sed -n 's/^AWG_MANAGEMENT_BIND=//p' "$DIR/.env" 2>/dev/null | head -1)
fi
MANAGEMENT_BIND=${MANAGEMENT_BIND:-0.0.0.0}
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    docker network create --driver bridge "$NETWORK" >/dev/null
    network_created=1
fi
subnet=$(docker network inspect --format '{{range .IPAM.Config}}{{if .Subnet}}{{.Subnet}}{{end}}{{end}}' "$NETWORK")
# Preserve a private legacy data bind instead of widening it during migration.
set --
if [ ! -f "$DIR/node-config/envelope.json" ] && [ -n "$UDP_BIND" ]; then
    set -- --bind-address "$UDP_BIND"
    if [ "$UDP_BIND" = 0.0.0.0 ]; then set -- "$@" --bind-address ::; fi
fi
values=$(docker run --rm --network none -v "$DIR:/install" \
    -v "$temporary/provision_managed_node.py:/provision_managed_node.py:ro" \
    --entrypoint python "$AGENT_IMAGE" /provision_managed_node.py /install "$NODE_ID" "$MANAGEMENT_PORT" "$subnet" "$@")
backend=$(printf '%s\n' "$values" | sed -n 's/^NODE_BACKEND_IP=//p')
[ -n "$backend" ] || { echo 'Managed backend provisioning failed' >&2; exit 1; }
# Reject a static address occupied by an unrelated container; never evict it.
occupants=$(docker network inspect --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{println}}{{end}}' "$NETWORK")
printf '%s\n' "$occupants" | while read -r name address; do
    if [ "${address%%/*}" = "$backend" ] && [ "$name" != awg-node-awg-agent-1 ]; then
        echo 'Managed backend address belongs to another container; installation unchanged' >&2
        exit 1
    fi
done
backup=$(mktemp -d "$DIR/managed-backup-XXXXXXXX")
chmod 700 "$backup"
# Kept for an operator-assisted rollback after this installer process has exited.
printf 'AWG_AGENT_IMAGE=%s\nAWG_INGRESS_IMAGE=%s\n' "$previous_agent_image" "$previous_ingress_image" > "$backup/previous-images.env"
chmod 600 "$backup/previous-images.env"
for file in compose.yaml .env trusttunnel.compose.yaml; do
    [ ! -f "$DIR/$file" ] || cp -p "$DIR/$file" "$backup/$file"
done
if [ -f "$DIR/.env" ]; then
    awk -F= '$1 !~ /^(AWG_SOURCE_DIR|AWG_AGENT_IMAGE|AWG_INGRESS_IMAGE|AWG_MANAGEMENT_PORT|AWG_MANAGEMENT_BIND|NODE_MANAGED|NODE_BRIDGE_NETWORK|NODE_BACKEND_IP)$/' \
        "$DIR/.env" > "$temporary/env"
else : > "$temporary/env"; fi
{
    printf 'AWG_SOURCE_DIR=%s\nAWG_AGENT_IMAGE=%s\nAWG_INGRESS_IMAGE=%s\n' "$SOURCE" "$AGENT_IMAGE" "$INGRESS_IMAGE"
    printf 'AWG_MANAGEMENT_PORT=%s\nAWG_MANAGEMENT_BIND=%s\n' "$MANAGEMENT_PORT" "$MANAGEMENT_BIND"
    printf 'NODE_MANAGED=1\nNODE_BRIDGE_NETWORK=%s\nNODE_BACKEND_IP=%s\n' "$NETWORK" "$backend"
} >> "$temporary/env"
changed=1
# A retry may recover committed ingress bindings. Release this project's legacy
# Docker port publications before ingress restores those sockets; other services
# on the server are never stopped. The EXIT trap restarts the old plan on failure.
if [ -f "$DIR/compose.yaml" ] && [ -f "$DIR/.env" ]; then
    old_dc stop awg-agent
fi
# Each visible file replacement is atomic; the backup covers failures between them.
cp "$temporary/compose.yaml" "$DIR/.managed-compose-$$"
cp "$temporary/env" "$DIR/.managed-env-$$"
chmod 600 "$DIR/.managed-env-$$"
mv "$DIR/.managed-compose-$$" "$DIR/compose.yaml"
mv "$DIR/.managed-env-$$" "$DIR/.env"
say "Starting managed node $NODE_ID (one-time migration may briefly interrupt existing tunnels)"
export AWG_AGENT_IMAGE="$AGENT_IMAGE" AWG_INGRESS_IMAGE="$INGRESS_IMAGE"
dc up -d --wait --wait-timeout 150 --pull never --force-recreate --remove-orphans
changed=0
say 'Done: protocols, free TCP/UDP ports and ACME are now managed from the TUI'
printf 'Management: TCP %s. Existing state volumes and node identity retained.\n' "$MANAGEMENT_PORT"
printf 'Image updates: docker compose -f "%s/compose.yaml" --env-file "%s/.env" pull\n' "$DIR" "$DIR"
printf 'Then: docker compose -f "%s/compose.yaml" --env-file "%s/.env" up -d\n' "$DIR" "$DIR"
printf 'Host/provider firewall and DNS must already allow the chosen ingress. Backup: %s\n' "$backup"
