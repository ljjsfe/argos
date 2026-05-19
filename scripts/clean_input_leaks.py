"""L4: remove leaked agent-output artifacts from public/input/task_*/.

Uses the same blacklist patterns as the Profiler (single source of truth).
Reports what would be deleted in dry-run mode; pass --apply to actually
remove. After this, future runs will be on clean inputs matching what
Phase 2 submission containers would see.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dataline.profiler.manifest import (
    _reserved_artifact_reason,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="public/input",
                    help="Directory containing task_<id>/ subdirs to clean.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete files. Without this flag, dry-run only.")
    args = ap.parse_args()

    root = (REPO / args.root).resolve()
    if not root.exists():
        sys.exit(f"Missing: {root}")

    tasks = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("task_"))
    print(f"Scanning {len(tasks)} task dirs under {root}\n")

    total_files = 0
    total_dirs = 0
    per_task: dict[str, list[str]] = {}

    for tdir in tasks:
        flagged: list[Path] = []
        for path in tdir.rglob("*"):
            if path.is_file():
                rel = path.relative_to(tdir)
                if _reserved_artifact_reason(rel) is not None:
                    flagged.append(path)
        if flagged:
            per_task[tdir.name] = [str(p.relative_to(tdir)) for p in flagged]
            total_files += len(flagged)
            if args.apply:
                for p in flagged:
                    try:
                        p.unlink()
                    except OSError as e:
                        print(f"  could not delete {p}: {e}")
                # Remove any now-empty reserved directories.
                for sub in ("output", "workspace", "temp"):
                    candidate = tdir / sub
                    if candidate.exists() and candidate.is_dir():
                        try:
                            shutil.rmtree(candidate)
                            total_dirs += 1
                        except OSError:
                            pass

    if not per_task:
        print("No leaked artifacts found.")
        return

    for task, files in per_task.items():
        print(f"{task}: {len(files)} file(s)")
        for f in files:
            print(f"    {f}")
    print()
    if args.apply:
        print(f"DELETED {total_files} files and pruned {total_dirs} reserved dirs.")
    else:
        print(f"DRY-RUN: would delete {total_files} files across {len(per_task)} tasks.")
        print(f"Re-run with --apply to actually delete.")


if __name__ == "__main__":
    main()
