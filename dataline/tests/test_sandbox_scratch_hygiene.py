"""Tests for Sandbox `_build_scratch` reusing the Profiler hygiene filter.

The L3.5 audit (2026-05-19) found that `_build_scratch()` was symlinking
every top-level entry under `task_dir` into the scratch cwd, including
agent-output artifacts (`output/`, `result.json`, `prediction.csv`) and
self-injected caches (`.dataline_cache/`). LLM-generated code could then
`open("output/result.json")` and bypass the L3 manifest blacklist entirely.

The fix imports `_reserved_artifact_reason` from the Profiler so the scratch
dir is filtered with the same single source of truth — and additionally
skips dot-prefixed entries.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dataline.core.sandbox import Sandbox


def _write(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


class TestScratchHygiene:
    def _make_sandbox(self, task_dir: Path) -> Sandbox:
        _write(task_dir / "task.json", '{"task_id":"x","question":"?"}')
        return Sandbox(task_dir=str(task_dir), timeout=30)

    def test_skips_dot_dirs(self, tmp_path):
        td = tmp_path / "task"
        _write(td / "context/csv/x.csv", "a\n1\n")
        _write(td / ".dataline_cache/doc_glossary/abc.json", "{}")
        _write(td / ".git/HEAD", "ref")
        sb = self._make_sandbox(td)
        try:
            scratch = Path(sb._build_scratch())
            names = {p.name for p in scratch.iterdir()}
            assert "context" in names
            assert ".dataline_cache" not in names
            assert ".git" not in names
        finally:
            shutil.rmtree(sb.temp_dir, ignore_errors=True)

    def test_skips_reserved_outputs(self, tmp_path):
        td = tmp_path / "task"
        _write(td / "context/csv/x.csv", "a\n1\n")
        _write(td / "output/prediction.csv", "x\n")
        _write(td / "result.json", "{}")
        _write(td / "prediction.csv", "x\n")
        _write(td / "trace.json", "{}")
        sb = self._make_sandbox(td)
        try:
            scratch = Path(sb._build_scratch())
            names = {p.name for p in scratch.iterdir()}
            assert "context" in names
            assert "output" not in names
            assert "result.json" not in names
            assert "prediction.csv" not in names
            assert "trace.json" not in names
        finally:
            shutil.rmtree(sb.temp_dir, ignore_errors=True)

    def test_legitimate_files_present(self, tmp_path):
        td = tmp_path / "task"
        _write(td / "context/csv/data.csv", "a,b\n1,2\n")
        _write(td / "context/knowledge.md", "# notes\n")
        _write(td / "context/db/x.db", "")  # zero-byte sqlite, still passes scratch filter
        sb = self._make_sandbox(td)
        try:
            scratch = Path(sb._build_scratch())
            # 2026-05-20 audit fix: scratch now uses deep tree-walk per-file
            # symlinks (not top-level directory symlinks) so reserved
            # filenames at any depth are filtered. As a side-effect:
            # - `context/` is no longer a single symlink but a real directory
            #   recreated under scratch with its filtered contents
            # - `task.json` is excluded (matches Profiler convention; task
            #   metadata is not exposed as user-readable data)
            assert (scratch / "context" / "csv" / "data.csv").exists()
            assert (scratch / "context" / "knowledge.md").exists()
            assert not (scratch / "task.json").exists()
        finally:
            shutil.rmtree(sb.temp_dir, ignore_errors=True)

    def test_hardlink_blocks_readlink_path_leak(self):
        """2026-05-20 audit Finding 1: with symlinks, agent code could
        `os.readlink('data.csv')` to recover the raw task_dir absolute
        path and then read gold.csv. Hardlinks make os.readlink raise
        EINVAL — the absolute path is not exposed."""
        import tempfile
        td = Path(tempfile.mkdtemp(prefix="readlink_test_"))
        try:
            _write(td / "context/csv/data.csv", "a\n1\n")
            _write(td / "gold.csv", "SECRET\n")
            sb = self._make_sandbox(td)
            try:
                scratch = Path(sb._build_scratch())
                materialized = scratch / "context" / "csv" / "data.csv"
                assert materialized.exists()
                # 1. Not a symlink — readlink should raise.
                assert not materialized.is_symlink()
                with pytest.raises(OSError):
                    import os
                    os.readlink(str(materialized))
                # 2. But the file is still readable as expected.
                assert materialized.read_text().strip() == "a\n1"
                # 3. Same inode as the source = it's a hardlink, not a copy.
                import os as _os
                assert _os.stat(materialized).st_ino == _os.stat(td / "context/csv/data.csv").st_ino
            finally:
                shutil.rmtree(sb.temp_dir, ignore_errors=True)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_late_added_extra_csv_links_propagate_on_rebuild(self):
        """2026-05-20 audit Finding 2: extra_csv_links set AFTER the first
        execute() were silently dropped because scratch was cached. Fix:
        every _build_scratch() call re-syncs extra_csv_links."""
        import tempfile
        td = Path(tempfile.mkdtemp(prefix="late_csv_test_"))
        try:
            _write(td / "context/data.csv", "a\n1\n")
            sb = self._make_sandbox(td)
            try:
                # First build with no extras
                scratch = sb._build_scratch()
                # Late-add an extra link
                fake_csv = td / "narrative_late_src.csv"
                fake_csv.write_text("x\n42\n")
                sb.extra_csv_links = [("narrative_late", str(fake_csv))]
                # Re-build (cache hit + sync)
                sb._build_scratch()
                late = Path(scratch) / "narrative_late.csv"
                assert late.exists(), "late-added extra CSV should propagate"
                assert late.read_text().strip() == "x\n42"
            finally:
                shutil.rmtree(sb.temp_dir, ignore_errors=True)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_deep_walk_blocks_reserved_at_any_depth(self):
        """2026-05-20 audit Finding 1 regression: scratch must filter
        reserved filenames inside non-reserved subdirs (e.g. context/
        is allowed but context/gold.csv must NOT be exposed)."""
        import tempfile
        td = Path(tempfile.mkdtemp(prefix="deep_walk_test_"))
        try:
            _write(td / "context/csv/data.csv", "a\n1\n")
            _write(td / "context/gold.csv", "answer\nSECRET\n")
            _write(td / "context/ground_truth.csv", "answer\nSECRET\n")
            _write(td / "context/solution.json", '{"answer": 42}')
            _write(td / "task.json", '{"task_id": "x"}')
            sb = self._make_sandbox(td)
            try:
                scratch = Path(sb._build_scratch())
                # legit
                assert (scratch / "context" / "csv" / "data.csv").exists()
                # leakage paths must be blocked even though they're inside
                # a non-reserved subdir
                assert not (scratch / "context" / "gold.csv").exists()
                assert not (scratch / "context" / "ground_truth.csv").exists()
                assert not (scratch / "context" / "solution.json").exists()
            finally:
                shutil.rmtree(sb.temp_dir, ignore_errors=True)
        finally:
            shutil.rmtree(td, ignore_errors=True)
