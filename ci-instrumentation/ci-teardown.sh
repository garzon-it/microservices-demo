#!/usr/bin/env bash
# OTel teardown: export job span, stop collector. Run this always, even on failure.

set -euo pipefail

python3 ci-instrumentation/ciwrap.py --finish-trace

# SIGTERM lets the collector flush pending batches before exiting.
set +e
pidfile="artifacts/otel/otelcol.pid"
[ -f "$pidfile" ] || exit 0
pid=$(cat "$pidfile")
kill "$pid" 2>/dev/null
timeout 30 tail --pid="$pid" -f /dev/null 2>/dev/null || true
