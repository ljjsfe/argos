# KDD Cup 2026 — Submission Workflow

End-to-end recipe to package the agent into a submittable Docker
archive, validate it locally, and ship it via Google Drive + email
per spec §3.

> Files live under: `Dockerfile`, `submit_main.py`, `.dockerignore`,
> `scripts/build_submission.sh`, `scripts/test_submission_locally.sh`.

---

## 0. Prerequisites

| Thing | Why |
|---|---|
| Docker Desktop / Engine ≥ 24.x with `buildx` | linux/amd64 cross-build on ARM macs |
| `gzip` | archive compression (built-in everywhere) |
| ~5 GB free disk | image build + tarball |
| Team ID assigned by organizers (e.g. `team0042`) | spec §3.1 |
| Team-leader email used at registration | spec §3.0 last bullet |
| Google Drive account that allows public sharing | spec §3.3 |

---

## 1. Build the image

```bash
# scripts/build_submission.sh <team_id> <version_int>
scripts/build_submission.sh team0042 1
```

The script:
1. `docker buildx build --platform=linux/amd64 -t team0042:v1 --load .`
2. Verifies the image is `amd64/linux`.
3. `docker save team0042:v1 | gzip > team0042_v1.tar.gz`
4. Refuses if archive > 10 GB (spec §3.2).
5. Refuses to overwrite an existing `_v<N>.tar.gz` (spec §3.1).

Expected output: `team0042_v1.tar.gz` ~ 700 MB – 1.5 GB.

---

## 2. Local validation (highly recommended)

```bash
# 3 tasks first (fast smoke):
scripts/test_submission_locally.sh team0042 1 3

# Or full local sweep:
scripts/test_submission_locally.sh team0042 1
```

The local test mirrors the evaluator's `docker run` invocation
(spec §3.4) — same mounts, env-vars, CPU/memory caps. It uses the
public DashScope endpoint (your `DASHSCOPE_API_KEY` from `.env`)
since the internal eval endpoint isn't reachable from your machine.

After the run:
```bash
ls results/local_submission_test_*/task_*/prediction.csv
cat results/local_submission_test_*/logs/runtime.log
```

Every task should have a `prediction.csv`. Even on per-task error
the agent writes a placeholder CSV — the eval pipeline never sees a
missing file.

### Pre-flight checks (matches spec §3.0 checklist)

```bash
# 1. Image + archive names match <team_id>:v<N> / <team_id>_v<N>.tar.gz
ls team0042_v1.tar.gz                    # exists, exact name
docker image inspect team0042:v1 --format='{{.RepoTags}}'

# 2. Was the archive built with `docker save` (not `docker export`)?
#    (Build script enforces this; if you did it manually, double-check.)

# 3. Architecture
docker image inspect team0042:v1 --format='{{.Architecture}}/{{.Os}}'
# → must print:  amd64/linux

# 4. ENTRYPOINT set, no extra args needed
docker image inspect team0042:v1 --format='{{.Config.Entrypoint}}'

# 5. Output paths land under /output/<task_id>/prediction.csv with no extra nesting
find results/local_submission_test_*/task_* -maxdepth 2 -name prediction.csv

# 6. CSVs parse as UTF-8
python -c "
import pandas as pd, glob
for p in glob.glob('results/local_submission_test_*/task_*/prediction.csv'):
    pd.read_csv(p)
print('all CSVs parsed OK')
"
```

---

## 3. Upload to Google Drive

1. Upload `team0042_v1.tar.gz` to Drive.
2. Right-click → **Share** → set to **Anyone with the link** → role
   **Viewer**.
3. **Verify** the link works without login:
   ```bash
   pip install gdown   # one-time
   gdown "https://drive.google.com/file/d/<FILE_ID>/view?usp=share_link" \
         -O /tmp/verify_team0042_v1.tar.gz
   ```
   If `gdown` errors with "Permission denied", sharing is wrong.

---

## 4. Send the submission email

**From:** the team-leader email used at registration (spec §3.0).
**To:** `kddcup@hkust-gz.edu.cn`
**Subject:** `[KDDCup2026 Data Agents] Submission - team0042 - v1`

**Body:**
```
Team ID: team0042
Version: v1
Sharing link: https://drive.google.com/file/d/<FILE_ID>/view?usp=share_link
```

The build script prints this template at the end of every successful
build — copy/paste it.

---

## 5. After submission

- **Don't delete or move** the Drive file until you receive the
  evaluator's completion notice (spec §3.3).
- If the evaluation fails and the organizers share `/logs/runtime.log`
  excerpts, audit them: nothing in `runtime.log` should leak gold
  answers or test inputs (spec §3.7).

---

## 6. Iterating: v2, v3, ...

Each rebuild **must** increment `<N>`:
```bash
scripts/build_submission.sh team0042 2
```

Previously submitted version numbers cannot be reused.

---

## 7. Image size budget

Current image (with tesseract + poppler + all python deps): ~1 GB
unzipped, ~400-700 MB gzipped. Well under the 10 GB cap.

If you add multimodal weights later (e.g. SmolVLM ~1.5 GB) you'll
still have ~7 GB headroom. Keep the existing slim base.

---

## 8. Spec compliance summary

| Spec § | Requirement | Where it's enforced |
|---|---|---|
| §3.0  | docker save, not export | `build_submission.sh` |
| §3.0  | linux/amd64 manifest | `Dockerfile` `--platform`, build script verify |
| §3.0  | /output/task_<id>/prediction.csv only | `submit_main.py` `OUTPUT_ROOT / task_id / "prediction.csv"` |
| §3.0  | UTF-8 CSV | `save_prediction` (existing helper) |
| §3.1  | Image + archive name format | `build_submission.sh` arg parsing |
| §3.1  | No version reuse | build script refuses overwrite |
| §3.2  | ≤ 10 GB archive | build script size check |
| §3.2  | ENTRYPOINT set, no extra args | `Dockerfile` `ENTRYPOINT [...]` |
| §3.4  | env-injected MODEL_API_URL etc. | `dataline/core/llm_client.py:create_client_from_config` |
| §3.7  | /logs/runtime.log via tee | `Dockerfile` ENTRYPOINT pipe |
