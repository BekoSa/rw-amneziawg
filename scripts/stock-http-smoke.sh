#!/bin/sh
# Stock/removal HTTP evidence on Docker bridges; no live/TUN services are selected.
set -eu
cd "$(dirname "$0")/.."
compose() { docker compose -f deploy/dev/compose.yaml "$@"; }
bootstrap() {
    docker run --rm -i --network none --cap-drop ALL --security-opt no-new-privileges --read-only \
        --tmpfs /tmp -v "$PWD:/workspace:ro" -w /workspace awg-dev-tooling:local "$@"
}
compose --profile '*' config --format json | bootstrap python scripts/check_network_safety.py "$PWD"
mkdir -p artifacts
mkdir artifacts/lab.lock || { echo 'Another lab operation is active' >&2; exit 1; }
evidence=artifacts/stock-http-run-$(date +%Y%m%dT%H%M%S)-$$
mkdir -p "$evidence"
finish() {
    status=$?
    trap - EXIT INT TERM
    compose logs --no-color > "$evidence/services.log" 2>&1 || status=1
    compose --profile '*' down > "$evidence/cleanup.log" 2>&1 || status=1
    scripts/host-snapshot.sh "$evidence/host-after" || status=1
    bootstrap python scripts/compare_host.py "$evidence/host-before" "$evidence/host-after" \
        > "$evidence/host-comparison.log" 2>&1 || status=1
    cat "$evidence/host-comparison.log"
    printf 'Stock HTTP removal smoke exit: %s\nNo TUN/live services. Full AWG restoration requires prepared runner.\n' "$status" > "$evidence/verification.md"
    rmdir artifacts/lab.lock || status=1
    exit "$status"
}
trap finish EXIT INT TERM
scripts/host-snapshot.sh "$evidence/host-before"
compose --profile '*' config --format json | bootstrap python scripts/check_pool_overlap.py "$evidence/host-before"
compose up -d --wait awg-db remnawave stock-proxy
compose run --rm -T tooling python -c '
import httpx,time
deadline=time.monotonic()+60
while time.monotonic()<deadline:
    try:
        httpx.get("http://stock-proxy:8080/api/auth/status",trust_env=False,timeout=3).raise_for_status()
        print("Stock public HTTP ready")
        break
    except httpx.HTTPError:
        time.sleep(.5)
else:
    raise RuntimeError("Stock HTTP readiness timeout")'
compose run --rm -T pki-init
compose run --rm -T stock-bootstrap
compose up -d --wait controller gateway
phase() {
    compose run --rm -T -v awg-dev_lab-credentials:/lab-credentials:ro \
        -e "STOCK_HTTP_PHASE=$1" tooling pytest -p no:cacheprovider -q tests/integration/test_stock_removal_http.py \
        > "$evidence/$1.log" 2>&1
    cat "$evidence/$1.log"
}
phase before
compose stop gateway controller
phase removed
compose up -d --wait controller gateway
phase restored
