#!/usr/bin/env bash
# Detect the CI provider and export normalized CI_* env vars used by the
# OTel Collector config and the ciwrap Python scripts.

set -euo pipefail

if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    CI_PROVIDER="github_actions"
    CI_PIPELINE_NAME="${GITHUB_WORKFLOW:-}"
    CI_JOB_NAME="${GITHUB_JOB:-}"
    CI_RUN_ID="${GITHUB_RUN_ID:-}"
    CI_RUN_ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
    CI_COMMIT_SHA="${GITHUB_SHA:-}"
    CI_REF_NAME="${GITHUB_REF_NAME:-}"
    CI_REF_TYPE="${GITHUB_REF_TYPE:-}"
    CI_REPOSITORY="${GITHUB_REPOSITORY:-}"
    CI_RUNNER_ID="${RUNNER_NAME:-}"
    CI_WORKER_TYPE="${RUNNER_ENVIRONMENT:-}"
    CI_RUNNER_OS="$(echo "${RUNNER_OS:-}" | tr '[:upper:]' '[:lower:]')"
    CI_RUNNER_ARCH="${RUNNER_ARCH:-}"
    CI_WORKSPACE="${GITHUB_WORKSPACE:-$(pwd)}"
    CI_PIPELINE_RUN_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
    CI_COMMIT_URL="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/commit/${GITHUB_SHA}"

elif [ "${GITLAB_CI:-}" = "true" ]; then
    CI_PROVIDER="gitlab_ci"
    CI_PIPELINE_NAME="${CI_PIPELINE_NAME:-${CI_PROJECT_NAME:-}}"
    CI_JOB_NAME="${CI_JOB_NAME:-}"
    CI_RUN_ID="${CI_PIPELINE_ID:-}"
    # GitLab has no re-run attempt counter; each re-run creates a new pipeline.
    CI_RUN_ATTEMPT="1"
    CI_COMMIT_SHA="${CI_COMMIT_SHA:-}"
    CI_REF_NAME="${CI_COMMIT_REF_NAME:-}"
    CI_REF_TYPE="${CI_COMMIT_TAG:+tag}"
    CI_REF_TYPE="${CI_REF_TYPE:-branch}"
    CI_REPOSITORY="${CI_PROJECT_PATH:-}"
    # Prefer human-readable description; fall back to short token then numeric ID.
    _gl_runner_numeric_id="${CI_RUNNER_ID:-}"
    CI_RUNNER_ID="${CI_RUNNER_DESCRIPTION:-${CI_RUNNER_SHORT_TOKEN:-runner-${_gl_runner_numeric_id:-unknown}}}"
    if [ "${CI_DISPOSABLE_ENVIRONMENT:-}" = "true" ]; then
        CI_WORKER_TYPE="cloud-hosted"
    else
        CI_WORKER_TYPE="self-hosted"
    fi
    CI_RUNNER_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
    # Normalize CI_RUNNER_EXECUTABLE_ARCH ("linux/amd64") to GitHub's format ("X64").
    case "${CI_RUNNER_EXECUTABLE_ARCH:-}" in
        *amd64|*x86_64) CI_RUNNER_ARCH="X64"   ;;
        *arm64|*aarch64) CI_RUNNER_ARCH="ARM64" ;;
        *arm)            CI_RUNNER_ARCH="ARM"   ;;
        *386)            CI_RUNNER_ARCH="X86"   ;;
        *)               CI_RUNNER_ARCH="${CI_RUNNER_EXECUTABLE_ARCH:-unknown}" ;;
    esac
    CI_WORKSPACE="${CI_PROJECT_DIR:-$(pwd)}"
    CI_PIPELINE_RUN_URL="${CI_PIPELINE_URL:-}"
    CI_COMMIT_URL="${CI_PROJECT_URL:-}/-/commit/${CI_COMMIT_SHA:-}"

else
    echo "WARNING: Unknown CI provider; CI_* vars will be empty." >&2
    CI_PROVIDER="unknown"
    CI_PIPELINE_NAME=""
    CI_JOB_NAME=""
    CI_RUN_ID=""
    CI_RUN_ATTEMPT="1"
    CI_COMMIT_SHA=""
    CI_REF_NAME=""
    CI_REF_TYPE=""
    CI_REPOSITORY=""
    CI_RUNNER_ID=""
    CI_WORKER_TYPE=""
    CI_RUNNER_OS=""
    CI_RUNNER_ARCH=""
    CI_WORKSPACE="$(pwd)"
    CI_PIPELINE_RUN_URL=""
    CI_COMMIT_URL=""
fi

# Always export so the collector process inherits these vars when sourced from ci-setup.sh.
export CI_PROVIDER CI_PIPELINE_NAME CI_JOB_NAME CI_RUN_ID CI_RUN_ATTEMPT \
       CI_COMMIT_SHA CI_REF_NAME CI_REF_TYPE CI_REPOSITORY CI_RUNNER_ID \
       CI_WORKER_TYPE CI_RUNNER_OS CI_RUNNER_ARCH CI_WORKSPACE CI_PIPELINE_RUN_URL \
       CI_COMMIT_URL

# On GitHub Actions, also persist to GITHUB_ENV so later steps see them too.
if [ "${GITHUB_ENV:-}" != "" ]; then
    cat >> "$GITHUB_ENV" <<EOF
CI_PROVIDER=$CI_PROVIDER
CI_PIPELINE_NAME=$CI_PIPELINE_NAME
CI_JOB_NAME=$CI_JOB_NAME
CI_RUN_ID=$CI_RUN_ID
CI_RUN_ATTEMPT=$CI_RUN_ATTEMPT
CI_COMMIT_SHA=$CI_COMMIT_SHA
CI_REF_NAME=$CI_REF_NAME
CI_REF_TYPE=$CI_REF_TYPE
CI_REPOSITORY=$CI_REPOSITORY
CI_RUNNER_ID=$CI_RUNNER_ID
CI_WORKER_TYPE=$CI_WORKER_TYPE
CI_RUNNER_OS=$CI_RUNNER_OS
CI_RUNNER_ARCH=$CI_RUNNER_ARCH
CI_WORKSPACE=$CI_WORKSPACE
CI_PIPELINE_RUN_URL=$CI_PIPELINE_RUN_URL
CI_COMMIT_URL=$CI_COMMIT_URL
EOF
fi

# Write CI_* vars to a file for shells that can't inherit them (GitLab after_script).
# ciwrap_traces.py reads this as a fallback when vars are missing from the environment.
mkdir -p "${CI_WORKSPACE}/artifacts/otel"
cat > "${CI_WORKSPACE}/artifacts/otel/ci-env.sh" <<EOF
export CI_PROVIDER="$CI_PROVIDER"
export CI_PIPELINE_NAME="$CI_PIPELINE_NAME"
export CI_JOB_NAME="$CI_JOB_NAME"
export CI_RUN_ID="$CI_RUN_ID"
export CI_RUN_ATTEMPT="$CI_RUN_ATTEMPT"
export CI_COMMIT_SHA="$CI_COMMIT_SHA"
export CI_REF_NAME="$CI_REF_NAME"
export CI_REF_TYPE="$CI_REF_TYPE"
export CI_REPOSITORY="$CI_REPOSITORY"
export CI_RUNNER_ID="$CI_RUNNER_ID"
export CI_WORKER_TYPE="$CI_WORKER_TYPE"
export CI_RUNNER_OS="$CI_RUNNER_OS"
export CI_RUNNER_ARCH="$CI_RUNNER_ARCH"
export CI_WORKSPACE="$CI_WORKSPACE"
export CI_PIPELINE_RUN_URL="$CI_PIPELINE_RUN_URL"
export CI_COMMIT_URL="$CI_COMMIT_URL"
EOF

echo "CI provider: $CI_PROVIDER (job=$CI_JOB_NAME, run=$CI_RUN_ID)"
