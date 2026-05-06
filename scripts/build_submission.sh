#!/usr/bin/env bash
# KDD Cup 2026 — build + verify submission image.
#
# Usage:
#   scripts/build_submission.sh <team_id> <version_int>
# Example:
#   scripts/build_submission.sh team0042 1
#
# Produces:
#   <team_id>_v<N>.tar.gz   (≤ 10 GB per spec §3.2)
# And prints the email template per spec §3.3.

set -euo pipefail

TEAM_ID="${1:-}"
VERSION="${2:-}"

if [[ -z "$TEAM_ID" || -z "$VERSION" ]]; then
    echo "Usage: $0 <team_id> <version_int>" >&2
    echo "Example: $0 team0042 1" >&2
    exit 1
fi

if [[ ! "$VERSION" =~ ^[0-9]+$ ]]; then
    echo "Error: version must be a positive integer (got '$VERSION')" >&2
    exit 1
fi

if [[ ! "$TEAM_ID" =~ ^team[0-9]+$ ]]; then
    echo "Warning: TEAM_ID '$TEAM_ID' does not match expected pattern teamNNNN" >&2
fi

IMAGE="${TEAM_ID}:v${VERSION}"
ARCHIVE="${TEAM_ID}_v${VERSION}.tar.gz"

# Refuse to overwrite a previous submission archive — submitted version
# numbers cannot be reused per spec §3.1.
if [[ -f "$ARCHIVE" ]]; then
    echo "Error: $ARCHIVE already exists. Increment <version_int>." >&2
    exit 1
fi

echo "==> Building image $IMAGE for linux/amd64..."
# buildx ensures we get a real linux/amd64 manifest even on ARM hosts.
docker buildx build \
    --platform=linux/amd64 \
    -t "$IMAGE" \
    --load \
    .

echo "==> Inspecting image..."
docker image inspect "$IMAGE" --format '
  Image      : {{.Id}}
  Size (b)   : {{.Size}}
  Arch       : {{.Architecture}} / {{.Os}}
  Cmd / Entry: {{.Config.Cmd}} / {{.Config.Entrypoint}}'

ARCH=$(docker image inspect "$IMAGE" --format '{{.Architecture}}/{{.Os}}')
if [[ "$ARCH" != "amd64/linux" ]]; then
    echo "Error: image is $ARCH, must be amd64/linux" >&2
    exit 1
fi

echo "==> Saving + gzipping → $ARCHIVE..."
# Spec §3.0 + §3.4: docker save (NOT docker export). Pipe through gzip.
docker save "$IMAGE" | gzip > "$ARCHIVE"

SIZE_BYTES=$(stat -f%z "$ARCHIVE" 2>/dev/null || stat -c%s "$ARCHIVE")
SIZE_MB=$(( SIZE_BYTES / 1024 / 1024 ))
SIZE_GB=$(awk "BEGIN { printf \"%.2f\", $SIZE_BYTES / 1024 / 1024 / 1024 }")

echo "==> Archive size: ${SIZE_MB} MB (${SIZE_GB} GB)"

if (( SIZE_MB > 10240 )); then
    echo "FAIL: archive exceeds 10 GB cap (${SIZE_MB} MB > 10240)" >&2
    echo "      Slim the image (smaller base, fewer deps, multi-stage)." >&2
    exit 1
fi

echo
echo "==> ✅ Submission archive ready: $ARCHIVE"
echo
cat <<EOF

────────────────────────────────────────────────────────────────────
NEXT STEPS (per spec §3.3):

1. Upload to Google Drive:
     gdrive upload "$ARCHIVE"          # or web UI

2. Set sharing → "Anyone with the link" → Viewer (per spec §3.0).

3. Verify the link works WITHOUT login:
     gdown "<your-share-link>" -O /tmp/verify_$ARCHIVE
     # should download successfully without auth.

4. Send email FROM THE TEAM-LEADER ADDRESS to:
     kddcup@hkust-gz.edu.cn

   Subject:
     [KDDCup2026 Data Agents] Submission - ${TEAM_ID} - v${VERSION}

   Body:
     Team ID: ${TEAM_ID}
     Version: v${VERSION}
     Sharing link: <paste your Google Drive share URL>

5. Do NOT delete or modify the file on Drive until you receive the
   evaluation completion notice (spec §3.3).
────────────────────────────────────────────────────────────────────
EOF
