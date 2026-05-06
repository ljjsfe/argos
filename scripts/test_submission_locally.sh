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

# Optional: temp /input subset for fast smoke tests.
# Using cp -r (not symlinks) because Docker mounts don't follow host-side
# symlinks pointing outside the mounted root.
if [[ -n "$N_TASKS" ]]; then
    SUBSET_DIR="${OUTPUT_DIR}/_input_subset"
    mkdir -p "$SUBSET_DIR"
    # shellcheck disable=SC2012
    ls -d "$INPUT_DIR"/task_* | head -n "$N_TASKS" | while read -r d; do
        cp -R "$d" "$SUBSET_DIR/"
    done
    INPUT_DIR="$SUBSET_DIR"
    echo "==> Subset: ${N_TASKS} tasks copied to $INPUT_DIR"
fi

# Eval host has 16 vCPU + 64GB; local Mac usually has fewer. Detect and
# clamp so we don't fail before the agent even starts.
HOST_CPUS=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 8)
HOST_CPUS=$(( HOST_CPUS - 1 ))    # leave 1 CPU for the host
HOST_CPUS=$(( HOST_CPUS < 16 ? HOST_CPUS : 16 ))
HOST_MEM_GB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo $((64*1024*1024*1024))) / 1024 / 1024 / 1024 ))
HOST_MEM_GB=$(( HOST_MEM_GB > 64 ? 64 : HOST_MEM_GB - 4 ))   # leave 4 GB for host

echo "==> Local CPUs:${HOST_CPUS} Mem:${HOST_MEM_GB}g (eval has 16/64)"
echo "==> Running $IMAGE on $INPUT_DIR..."
docker run --rm \
    --platform=linux/amd64 \
    --cpus="${HOST_CPUS}" \
    --memory="${HOST_MEM_GB}g" \
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
