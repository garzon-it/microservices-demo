#!/usr/bin/env bash
# OTel setup: download collector, start it, init trace context.
# Requires Python + pip. CI_SERVICE_NAME and CI_JOB_TARGET must be set.

set -euo pipefail

mkdir -p artifacts/otel artifacts/step-logs artifacts/test-results

pip install --break-system-packages -r ci-instrumentation/requirements.txt

OTEL_VERSION="0.130.0"
curl -fsSL -o otelcol-contrib.tgz \
  "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTEL_VERSION}/otelcol-contrib_${OTEL_VERSION}_linux_amd64.tar.gz"
tar -xzf otelcol-contrib.tgz otelcol-contrib
chmod +x otelcol-contrib

# Source so CI_* vars are inherited by the collector process below.
# Also writes ci-env.sh for shells that can't inherit env (GitLab after_script).
. ci-instrumentation/setup_ci_context.sh

./otelcol-contrib --config ci-instrumentation/otelcol-ci.yaml > artifacts/otel/otelcol.log 2>&1 &
echo $! > artifacts/otel/otelcol.pid
disown  # detach from this subshell so it survives after ci-setup.sh exits

# Wait for the collector to be ready on port 4318 before sending traces.
sleep 3

python3 ci-instrumentation/ciwrap.py --init-trace
