#!/usr/bin/env bash
# Run the submission image against local public/input to verify it
# works before uploading to Google Drive.
#
# Usage:
#   scripts/test_submission_locally.sh <team_id> <version_int> [n_tasks]
# Example:
#   scripts/test_submission_locally.sh team0042 1 3   # only 3 tasks
#   scripts/test_submission_locally.sh team0042 1     # all 50 tasks
#
# Mirrors the evaluator's docker run command (spec §3.4) as closely
# as we can locally — same mount layout, same env-var injection.

set -euo pipefail

TEAM_ID="${1:?team_id required}"
VERSION="${2:?version required}"
N_TASKS="${3:-}"

IMAGE="${TEAM_ID}:v${VERSION}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Error: image $IMAGE not found. Run scripts/build_submission.sh first." >&2
    exit 1
fi

# Pull credentials from .env for the local model API.
# At eval time the evaluator injects MODEL_API_URL etc. directly.
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

: "${DASHSCOPE_API_KEY:?DASHSCOPE_API_KEY missing — needed for local test}"

INPUT_DIR="${PWD}/public/input"
OUTPUT_DIR="${PWD}/results/local_submission_test_$(date +%H%M%S)"
LOG_DIR="${OUTPUT_DIR}/logs"
mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

# Optional: temp /input subset for fast smoke tests
if [[ -n "$N_TASKS" ]]; then
    SUBSET_DIR="${OUTPUT_DIR}/_input_subset"
    mkdir -p "$SUBSET_DIR"
    # shellcheck disable=SC2012
    ls -d "$INPUT_DIR"/task_* | head -n "$N_TASKS" | while read -r d; do
        ln -s "$d" "$SUBSET_DIR/$(basename "$d")"
    done
    INPUT_DIR="$SUBSET_DIR"
    echo "==> Subset: ${N_TASKS} tasks at $INPUT_DIR"
fi

echo "==> Running $IMAGE on $INPUT_DIR..."
docker run --rm \
    --platform=linux/amd64 \
    --cpus=16 \
    --memory=64g \
    -v "${INPUT_DIR}:/input:ro" \
    -v "${OUTPUT_DIR}:/output:rw" \
    -v "${LOG_DIR}:/logs:rw" \
    -e MODEL_API_URL="https://dashscope.aliyuncs.com/compatible-mode/v1" \
    -e MODEL_API_KEY="$DASHSCOPE_API_KEY" \
    -e MODEL_NAME="qwen3.5-35b-a3b" \
    "$IMAGE"

echo
echo "==> Done. Outputs at $OUTPUT_DIR"
echo "==> Predictions:"
find "$OUTPUT_DIR" -name prediction.csv | head -10
echo "==> Log: $LOG_DIR/runtime.log"
echo "==> Tail of runtime log:"
tail -20 "$LOG_DIR/runtime.log" || echo "(no log produced)"
