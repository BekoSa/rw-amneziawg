#!/bin/sh
# Installs the AWG extension next to an already running stock Remnawave panel.
#
# What it touches outside its own directory (each after "yes" or --yes; the previous value of every
# changed key is recorded in $DIR/stock-changes and restored key-by-key by uninstall.sh):
#   * the stock subscription page .env: REMNAWAVE_PANEL_URL -> http://awg-gateway:8081
#   * the panel .env (only with --webhook yes / "y"): WEBHOOK_* (adds the extension URL, keeps existing ones)
# Only the changed service is re-created (`up -d --no-deps <service>`). Stock images and the Remnawave
# database are never modified.
set -eu

SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
DIR=/opt/awg-extension
NETWORK=''
TOKEN_FILE=''
SUBPAGE=ask
WEBHOOK=ask
ASSUME_YES=0
REGISTRY=''
IMAGE_TAG=latest

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [options]
  --dir PATH             install directory (default /opt/awg-extension)
  --network NAME         Docker network of the Remnawave panel (auto-detected)
  --api-token-file FILE  file with a Remnawave API token (otherwise asked interactively)
  --subpage yes|no       put the Gateway in front of the stock subscription page
  --webhook yes|no       enable Remnawave webhooks to the extension (otherwise polling every 15 s)
  --image-registry REPO  use CI images REPO/extension (e.g. ghcr.io/bekosa/rw-amneziawg) instead of building
  --image-tag TAG        tag of those images (default: latest)
  --yes                  accept defaults for all other questions
EOF
}

while [ $# -gt 0 ]; do
    case $1 in
        --dir) DIR=$2; shift 2 ;;
        --network) NETWORK=$2; shift 2 ;;
        --api-token-file) TOKEN_FILE=$2; shift 2 ;;
        --subpage) SUBPAGE=$2; shift 2 ;;
        --webhook) WEBHOOK=$2; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        --image-registry) REGISTRY=${2%/}; shift 2 ;;
        --image-tag) IMAGE_TAG=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
ask_yes() {  # ask_yes "question" preset(yes|no|ask) default(yes|no)
    case $2 in yes) return 0 ;; no) return 1 ;; esac
    if [ "$ASSUME_YES" = 1 ]; then [ "$3" = yes ]; return; fi
    printf '%s [%s] ' "$1" "$( [ "$3" = yes ] && echo 'Y/n' || echo 'y/N')"
    read -r answer
    case ${answer:-$3} in [Yy]*) return 0 ;; *) return 1 ;; esac
}
secret() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-40}"; }
container_env() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" | sed -n "s/^$2=//p" | head -1; }
compose_label() { docker inspect -f "{{index .Config.Labels \"com.docker.compose.$2\"}}" "$1"; }
restart_stock() {  # restart_stock CONTAINER: re-create only this service of its own compose project
    project=$(compose_label "$1" project)
    service=$(compose_label "$1" service)
    files=$(compose_label "$1" project.config_files)
    workdir=$(compose_label "$1" project.working_dir)
    set -- -p "$project" --project-directory "$workdir"
    old_ifs=$IFS; IFS=,
    for file in $files; do set -- "$@" -f "$file"; done
    IFS=$old_ifs
    docker compose "$@" up -d --no-deps "$service"
}
set_env() {  # set_env FILE KEY VALUE: replace or append; the value is never interpreted; atomic
    tmp=$(mktemp "$1.awg-XXXXXX")
    AWG_KEY=$2 AWG_VALUE=$3 awk 'BEGIN { k = ENVIRON["AWG_KEY"]; v = ENVIRON["AWG_VALUE"]; done = 0 }
        index($0, k "=") == 1 { print k "=" v; done = 1; next } { print }
        END { if (!done) print k "=" v }' "$1" > "$tmp"
    chmod --reference="$1" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    chown --reference="$1" "$tmp" 2>/dev/null || true
    mv "$tmp" "$1"
}
record_stock() {  # record_stock FILE KEY CONTAINER: remember the original value once, for uninstall.sh
    grep -q "^$(printf '%s' "$1|$2" | base64 | tr -d '\n')	" "$DIR/stock-changes" 2>/dev/null && return 0
    if grep -q "^$2=" "$1"; then
        original=$(sed -n "s/^$2=//p" "$1" | head -1 | base64 | tr -d '\n'); state=present
    else
        original=''; state=absent
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(printf '%s' "$1|$2" | base64 | tr -d '\n')" "$1" "$2" "$state" "$original" "$3" >> "$DIR/stock-changes"
    cp -p "$1" "$1.awg-backup-$(date +%Y%m%d%H%M%S)"  # extra manual-recovery copy, never used automatically
}
change_stock() {  # change_stock FILE KEY VALUE CONTAINER
    record_stock "$1" "$2" "$4"
    set_env "$1" "$2" "$3"
}

command -v docker >/dev/null || die 'Docker is required'
docker compose version >/dev/null 2>&1 || die 'Docker Compose v2 is required'

say 'Looking for stock Remnawave'
PANEL=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /^remnawave\/backend(:|$)/ {print $1; exit}')
[ -n "$PANEL" ] || die 'No running remnawave/backend container found. Install and start Remnawave first.'
if [ -z "$NETWORK" ]; then
    NETWORK=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$PANEL" | grep -v '^$' | head -1)
fi
PANEL_PORT=$(container_env "$PANEL" APP_PORT); PANEL_PORT=${PANEL_PORT:-3000}
say "Panel container: $PANEL, network: $NETWORK, port: $PANEL_PORT"
REMNAWAVE_API_URL="http://$PANEL:$PANEL_PORT/api"

mkdir -p "$DIR"
chmod 700 "$DIR"
ENV_FILE="$DIR/.env"
[ -f "$ENV_FILE" ] || : > "$ENV_FILE"
chmod 600 "$ENV_FILE"
cp "$SOURCE_DIR/deploy/production/compose.yaml" "$DIR/compose.yaml"
dc() { docker compose -f "$DIR/compose.yaml" --env-file "$ENV_FILE" "$@"; }
get_env() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1; }
keep_or() { value=$(get_env "$1"); [ -n "$value" ] && echo "$value" || echo "$2"; }

if [ -n "$TOKEN_FILE" ]; then
    API_TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")
elif [ -n "$(get_env REMNAWAVE_API_TOKEN)" ]; then
    API_TOKEN=$(get_env REMNAWAVE_API_TOKEN)
else
    echo 'Create an API token in Remnawave: Settings → API tokens (scope: all), and paste it here.'
    printf 'Remnawave API token: '; stty -echo 2>/dev/null || true; read -r API_TOKEN; stty echo 2>/dev/null || true; echo
fi
[ -n "$API_TOKEN" ] || die 'API token is required'

CONTROLLER_ID=$(keep_or AWG_CONTROLLER_ID "$(cat /proc/sys/kernel/random/uuid)")
for pair in \
    "AWG_SOURCE_DIR=$SOURCE_DIR" "REMNAWAVE_NETWORK=$NETWORK" "REMNAWAVE_API_URL=$REMNAWAVE_API_URL" \
    "REMNAWAVE_API_TOKEN=$API_TOKEN" "AWG_CONTROLLER_ID=$CONTROLLER_ID" \
    "AWG_GATEWAY_STOCK_URL=$(keep_or AWG_GATEWAY_STOCK_URL "http://$PANEL:$PANEL_PORT")" \
    "AWG_DATABASE_PASSWORD=$(keep_or AWG_DATABASE_PASSWORD "$(secret 32)")" \
    "AWG_ADMIN_TOKEN=$(keep_or AWG_ADMIN_TOKEN "$(secret 40)")" \
    "AWG_GATEWAY_TOKEN=$(keep_or AWG_GATEWAY_TOKEN "$(secret 40)")" \
    "AWG_KEY_ENCRYPTION_KEY=$(keep_or AWG_KEY_ENCRYPTION_KEY "$(head -c 32 /dev/urandom | base64)")" \
    "AWG_WEBHOOK_SECRET=$(keep_or AWG_WEBHOOK_SECRET "$(secret 64)")"; do
    set_env "$ENV_FILE" "${pair%%=*}" "${pair#*=}"
done

if [ -n "$REGISTRY" ]; then
    set_env "$ENV_FILE" AWG_EXTENSION_IMAGE "$REGISTRY/extension:$IMAGE_TAG"
    say "Pulling $REGISTRY/extension:$IMAGE_TAG"
    dc pull controller
else
    say 'Building the extension image (first run takes a few minutes)'
    dc build controller
fi
say 'Checking the API token and reading Internal Squads'
SQUADS=$(dc --profile tools run --rm -T check-remnawave) || die 'Remnawave rejected the API token (or is unreachable on the panel network)'
echo "$SQUADS" | awk -F '\t' 'BEGIN {print "   Internal Squads:"} {printf "   - %s (%s users)\n", $3, $2}'

# ---------------------------------------------------------------- subscriptions
STOCK_URL="http://$PANEL:$PANEL_PORT"
PASSTHROUGH=0
SUBPAGE_CONTAINER=$(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /^remnawave\/subscription-page(:|$)/ {print $1; exit}')
if [ -n "$SUBPAGE_CONTAINER" ]; then
    CURRENT=$(container_env "$SUBPAGE_CONTAINER" REMNAWAVE_PANEL_URL)
    SUBPAGE_DIR=$(compose_label "$SUBPAGE_CONTAINER" project.working_dir)
    say "Stock subscription page: $SUBPAGE_CONTAINER (REMNAWAVE_PANEL_URL=$CURRENT)"
    if [ "$CURRENT" = 'http://awg-gateway:8081' ]; then
        STOCK_URL=$(keep_or AWG_GATEWAY_STOCK_URL "$STOCK_URL"); PASSTHROUGH=1
        say 'Subscription page already goes through the Gateway'
    elif ask_yes 'Route the subscription page through the AWG Gateway (one line in its .env, backup kept)?' "$SUBPAGE" yes; then
        [ -f "$SUBPAGE_DIR/.env" ] || die "Cannot find $SUBPAGE_DIR/.env of the subscription page"
        docker network inspect "$NETWORK" -f '{{range .Containers}}{{println .Name}}{{end}}' | grep -qx "$SUBPAGE_CONTAINER" \
            || die "Subscription page is not on network $NETWORK"
        STOCK_URL=${CURRENT%/}; PASSTHROUGH=1
        ROUTE_SUBPAGE=1
    fi
else
    warn 'No stock subscription page container found.'
fi
set_env "$ENV_FILE" AWG_GATEWAY_STOCK_URL "$STOCK_URL"
set_env "$ENV_FILE" AWG_GATEWAY_SUBPAGE_PASSTHROUGH "$PASSTHROUGH"

# ---------------------------------------------------------------- start
say 'Creating the management PKI'
dc --profile tools run --rm -T pki-init
say 'Starting Controller, Gateway and database'
dc up -d --wait --pull never awg-db controller gateway
# Admin/internal Controller API: only from the extension's internal network (TUI, Gateway), not from
# other containers on the panel network.
AWG_SUBNET=$(docker network inspect "awg-extension_awg" -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' | awk '{print $1}')
if [ -n "$AWG_SUBNET" ] && [ "$(get_env AWG_ADMIN_ALLOWED_CIDRS)" != "$AWG_SUBNET" ]; then
    set_env "$ENV_FILE" AWG_ADMIN_ALLOWED_CIDRS "$AWG_SUBNET"
    dc up -d --wait --pull never controller
fi

if [ "${ROUTE_SUBPAGE:-0}" = 1 ]; then
    change_stock "$SUBPAGE_DIR/.env" REMNAWAVE_PANEL_URL http://awg-gateway:8081 "$SUBPAGE_CONTAINER"
    say 'Re-creating the stock subscription page with the new REMNAWAVE_PANEL_URL'
    restart_stock "$SUBPAGE_CONTAINER"
    if [ "$(container_env "$SUBPAGE_CONTAINER" REMNAWAVE_PANEL_URL)" != 'http://awg-gateway:8081' ]; then
        "$SOURCE_DIR/uninstall.sh" --dir "$DIR" --stock-only
        die "The subscription page sets REMNAWAVE_PANEL_URL inside its compose file, not .env. Change it there to http://awg-gateway:8081 and re-run."
    fi
fi

# ---------------------------------------------------------------- webhooks
PANEL_DIR=$(compose_label "$PANEL" project.working_dir)
HOOK=http://awg-controller:8080/webhooks/remnawave
if [ -f "$PANEL_DIR/.env" ] && ! grep -q "$HOOK" "$PANEL_DIR/.env" && \
   ask_yes 'Enable Remnawave webhooks for instant updates (panel .env, backup kept, panel restarts)?' "$WEBHOOK" no; then
    EXISTING_URL=$(sed -n 's/^WEBHOOK_URL=//p' "$PANEL_DIR/.env" | tr -d '"' | head -1)
    EXISTING_SECRET=$(sed -n 's/^WEBHOOK_SECRET_HEADER=//p' "$PANEL_DIR/.env" | tr -d '"' | head -1)
    if [ -n "$EXISTING_SECRET" ] && grep -Eq '^WEBHOOK_ENABLED="?true' "$PANEL_DIR/.env"; then
        set_env "$ENV_FILE" AWG_WEBHOOK_SECRET "$EXISTING_SECRET"  # Remnawave signs all webhook URLs with one secret
        change_stock "$PANEL_DIR/.env" WEBHOOK_URL "${EXISTING_URL:+$EXISTING_URL,}$HOOK" "$PANEL"
    else
        change_stock "$PANEL_DIR/.env" WEBHOOK_ENABLED true "$PANEL"
        change_stock "$PANEL_DIR/.env" WEBHOOK_URL "$HOOK" "$PANEL"
        change_stock "$PANEL_DIR/.env" WEBHOOK_SECRET_HEADER "$(get_env AWG_WEBHOOK_SECRET)" "$PANEL"
    fi
    dc up -d --wait --pull never controller
    say 'Restarting the panel to load webhook settings'
    restart_stock "$PANEL"
fi

# ---------------------------------------------------------------- admin command
{
    echo '#!/bin/sh'
    echo '# AWG extension terminal UI. Examples: ./awg   ./awg status   ./awg peers'
    echo 'tty=; [ -t 0 ] || tty=-T'
    printf 'exec docker compose -f %s --env-file %s --profile tools run --rm $tty tui python -m awg_tui "$@"\n' \
        "'$DIR/compose.yaml'" "'$ENV_FILE'"
} > "$DIR/awg"
chmod 700 "$DIR/awg"

say 'Done'
cat <<EOF

  Administration (terminal):   $DIR/awg
  1. In the TUI, tab "Ноды" → n: issue a node key, run the printed command on the VPN server
     (./install-node.sh from this repository), then press "Нода установлена — подключить".
  2. Tab "Профили" → n: choose the node, Internal Squads and AWG 3.1 parameters → Save → Validate → Apply.
  3. In Remnawave add users to the chosen squad — they get AWG in their subscription automatically.
  4. To show the AmneziaVPN key (vpn://) on the subscription page, enable "Show connection keys"
     in Remnawave's subscription page config (stock hides all raw links by default).
EOF
if [ "$PASSTHROUGH" = 0 ]; then
cat <<EOF

  Subscriptions: route /api/sub/* of your subscription domain to awg-gateway:8081 on network $NETWORK.
    Caddy:  handle /api/sub/* { reverse_proxy awg-gateway:8081 }
    nginx:  location /api/sub/ { proxy_pass http://awg-gateway:8081; proxy_set_header X-Forwarded-For \$remote_addr; proxy_set_header X-Forwarded-Proto https; }
EOF
fi
