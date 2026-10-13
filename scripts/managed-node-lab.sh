#!/bin/sh
# Host orchestration only. Runtime/dependencies execute in isolated Docker bridges.
set -eu
cd "$(dirname "$0")/.."
compose() { docker compose -f deploy/dev/compose.managed-node.yaml "$@"; }
bootstrap() {
    docker run --rm -i --network none --cap-drop ALL --security-opt no-new-privileges --read-only \
        --tmpfs /tmp:exec -v "$PWD:/workspace:ro" -w /workspace awg-dev-tooling:local "$@"
}
lint() { compose config --format json | bootstrap python scripts/check_network_safety.py "$PWD"; }
finish() {
    status=$?
    trap - EXIT INT TERM
    if [ "$action" = test ]; then
        compose logs --no-color > "$evidence/services.log" 2>&1 || status=1
        compose down >> "$evidence/cleanup.log" 2>&1 || status=1
    fi
    scripts/host-snapshot.sh "$evidence/host-after" || status=1
    bootstrap python scripts/compare_host.py "$evidence/host-before" "$evidence/host-after" \
        > "$evidence/host-comparison.log" 2>&1 || status=1
    cat "$evidence/host-comparison.log"
    printf '# Managed node lab verification\n\nAction: `%s`\n\nExit status: %s\n\nNetwork mode: isolated internal Docker bridges; no TUN/full tunnel.\n\nLogs: tests.log, services.log, host-comparison.log. Named state volume retained.\n' \
        "$action" "$status" > "$evidence/verification.md"
    rmdir artifacts/lab.lock || status=1
    exit "$status"
}
action=${1:-help}
[ "$#" -eq 0 ] || shift
case "$action" in
    build)
        lint
        docker build -f deploy/dev/Dockerfile.pebble -t awg-tt-pebble:local .
        docker build -f deploy/dev/Dockerfile.trusttunnel-client -t awg-tt-client:local .
        docker build -f deploy/dev/Dockerfile.managed-init -t awg-managed-init:local .
        docker build -f deploy/dev/Dockerfile.managed-client -t awg-managed-client:local .
        docker build -f apps/node-ingress/Dockerfile -t awg-dev-ingress:local .
        docker build -f deploy/dev/Dockerfile.managed-node -t awg-managed-agent:local .
        ;;
    lint) lint ;;
    test|down)
        lint
        mkdir -p artifacts
        mkdir artifacts/lab.lock || { echo 'Another lab operation is active' >&2; exit 1; }
        evidence=artifacts/managed-node-run-$(date +%Y%m%dT%H%M%S)-$$
        mkdir -p "$evidence"
        MANAGED_LAB_RUN=$(basename "$evidence")
        export MANAGED_LAB_RUN
        trap finish EXIT INT TERM
        scripts/host-snapshot.sh "$evidence/host-before"
        compose config --format json | bootstrap python scripts/check_pool_overlap.py "$evidence/host-before"
        if [ "$action" = down ]; then
            compose down
        else
            compose up -d pebble ingress managed-agent managed-client managed-target
            if [ "$#" -eq 0 ]; then set -- -q tests/integration/test_managed_node_lab.py; fi
            # Avoid a pipeline hiding pytest failure. Output is retained even on failure.
            status=0
            compose run --rm -T tooling pytest -p no:cacheprovider "$@" > "$evidence/tests.log" 2>&1 || status=$?
            cat "$evidence/tests.log"
            exit "$status"
        fi
        ;;
    *) echo 'Usage: scripts/managed-node-lab.sh build|lint|test [pytest args]|down'; exit 2 ;;
esac
