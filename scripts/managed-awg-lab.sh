#!/bin/sh
# CI prepared runner only; host TUN must already exist. All runtimes bridge-isolated.
set -eu
cd "$(dirname "$0")/.."
compose() { docker compose -f deploy/dev/compose.yaml -f deploy/dev/compose.live.yaml -f deploy/dev/compose.managed-awg.yaml "$@"; }
bootstrap() { docker run --rm -i --network none --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp -v "$PWD:/workspace:ro" -w /workspace awg-dev-tooling:local "$@"; }
lint() { compose --profile '*' config --format json | bootstrap python scripts/check_network_safety.py "$PWD"; }
phase() { echo "Managed AWG phase: $1"; compose run --rm -T -e "E2E_PHASE=$1" tooling pytest -p no:cacheprovider -q -m network tests/e2e; }
finish() {
    status=$?
    trap - EXIT INT TERM
    compose --profile '*' logs --no-color > "$evidence/services.log" 2>&1 || status=1
    compose --profile '*' down > "$evidence/cleanup.log" 2>&1 || status=1
    scripts/host-snapshot.sh "$evidence/host-after" || status=1
    bootstrap python scripts/compare_host.py "$evidence/host-before" "$evidence/host-after" > "$evidence/host-comparison.log" 2>&1 || status=1
    cat "$evidence/host-comparison.log"
    rmdir artifacts/lab.lock || status=1
    exit "$status"
}
e2e() {
    compose --profile stock up -d --wait remnawave stock-proxy
    compose run --rm -T pki-init
    compose run --rm -T stock-bootstrap
    compose --profile extension --profile live up -d --wait --force-recreate controller gateway managed-awg-ingress awg-agent awg-client target
    phase connect
    phase lifecycle
    compose stop controller
    phase controller_down
    compose up -d --wait controller
    compose restart awg-agent
    phase agent_restart
    compose restart controller
    compose up -d --wait controller
    phase controller_restart
    compose stop gateway controller awg-agent managed-awg-ingress
    phase removed
    compose --profile extension --profile live up -d --wait controller gateway managed-awg-ingress awg-agent
    phase restored
    phase delete
    echo 'MANAGED AWG E2E: PASS'
}
case ${1:-help} in
    build) scripts/lab.sh build; docker build -f deploy/dev/Dockerfile.managed-init -t awg-managed-init:local .; docker build -f apps/node-ingress/Dockerfile -t awg-dev-ingress:local . ;;
    lint) lint ;;
    e2e)
        scripts/live-preflight.sh
        lint
        mkdir -p artifacts
        mkdir artifacts/lab.lock || { echo 'Another lab operation is active' >&2; exit 1; }
        evidence=artifacts/managed-awg-run-$(date +%Y%m%dT%H%M%S)-$$
        scripts/host-snapshot.sh "$evidence/host-before"
        trap finish EXIT INT TERM
        compose --profile '*' config --format json | bootstrap python scripts/check_pool_overlap.py "$evidence/host-before"
        set +e
        (set -e; e2e) > "$evidence/e2e.log" 2>&1
        status=$?
        set -e
        cat "$evidence/e2e.log"
        exit "$status"
        ;;
    *) echo 'Usage: scripts/managed-awg-lab.sh build|lint|e2e'; exit 2 ;;
esac
