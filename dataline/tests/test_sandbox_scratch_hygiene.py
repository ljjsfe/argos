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
            names = {p.name for p in scratch.iterdir()}
            # All top-level legitimate entries should symlink through.
            assert "context" in names
            assert "task.json" in names  # task.json is not a reserved name
        finally:
            shutil.rmtree(sb.temp_dir, ignore_errors=True)
