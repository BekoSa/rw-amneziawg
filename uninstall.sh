#!/bin/sh
# Removes the AWG extension from the panel server and returns stock Remnawave to its previous settings.
# Only the .env keys install.sh changed are restored (from $DIR/stock-changes), so later edits by the
# administrator are kept. Data (AWG database, PKI) is kept unless --purge is given.
set -eu
DIR=/opt/awg-extension
PURGE=0
STOCK_ONLY=0
while [ $# -gt 0 ]; do
    case $1 in
        --dir) DIR=$2; shift 2 ;;
        --purge) PURGE=1; shift ;;
        --stock-only) STOCK_ONLY=1; shift ;;  # restore stock settings, leave the extension running
        *) echo 'Usage: sudo ./uninstall.sh [--dir PATH] [--purge]'; exit 2 ;;
    esac
done
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
[ -d "$DIR" ] || { echo "No installation in $DIR"; exit 1; }

label() { docker inspect -f "{{index .Config.Labels \"com.docker.compose.$2\"}}" "$1"; }
restart_stock() {
    docker inspect "$1" >/dev/null 2>&1 || { warn "Container $1 not found; restart it yourself to apply restored settings"; return 0; }
    project=$(label "$1" project); service=$(label "$1" service)
    files=$(label "$1" project.config_files); workdir=$(label "$1" project.working_dir)
    set -- -p "$project" --project-directory "$workdir"
    old_ifs=$IFS; IFS=,; for file in $files; do set -- "$@" -f "$file"; done; IFS=$old_ifs
    docker compose "$@" up -d --no-deps "$service"
}
restore_key() {  # restore_key FILE KEY STATE ORIGINAL_BASE64
    [ -f "$1" ] || { warn "$1 is gone; cannot restore $2"; return 0; }
    tmp=$(mktemp "$1.awg-XXXXXX")
    if [ "$3" = absent ]; then
        AWG_KEY=$2 awk 'index($0, ENVIRON["AWG_KEY"] "=") != 1' "$1" > "$tmp"
    else
        AWG_KEY=$2 AWG_VALUE=$(printf '%s' "$4" | base64 -d) awk '
            BEGIN { k = ENVIRON["AWG_KEY"]; v = ENVIRON["AWG_VALUE"] }
            index($0, k "=") == 1 { print k "=" v; next } { print }' "$1" > "$tmp"
    fi
    chmod --reference="$1" "$tmp" 2>/dev/null || chmod 600 "$tmp"
    chown --reference="$1" "$tmp" 2>/dev/null || true
    mv "$tmp" "$1"
    say "Restored $2 in $1"
}

CHANGES="$DIR/stock-changes"
if [ -s "$CHANGES" ]; then
    containers=''
    while IFS="$(printf '\t')" read -r _id file key state original container; do
        restore_key "$file" "$key" "$state" "$original"
        case " $containers " in *" $container "*) ;; *) containers="$containers $container" ;; esac
    done < "$CHANGES"
    for container in $containers; do restart_stock "$container"; done
    rm -f "$CHANGES"
fi
[ "$STOCK_ONLY" = 1 ] && exit 0

[ -f "$DIR/compose.yaml" ] || { echo "No extension compose file in $DIR"; exit 1; }
if [ "$PURGE" = 1 ]; then
    docker compose -f "$DIR/compose.yaml" --env-file "$DIR/.env" --profile tools down --volumes
    rm -rf "$DIR"
    say 'Extension and its data removed'
else
    docker compose -f "$DIR/compose.yaml" --env-file "$DIR/.env" --profile tools down
    say "Extension stopped; data kept in Docker volumes and $DIR (re-run install.sh to restore)"
fi
