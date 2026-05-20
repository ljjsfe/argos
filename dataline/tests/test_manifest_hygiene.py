"""Tests for Profiler input-hygiene rules (blacklist + empty DB detection)."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from dataline.profiler.manifest import (
    RESERVED_DIR_BASENAMES,
    RESERVED_FILENAMES,
    RESERVED_PREFIXES,
    RESERVED_SUFFIXES,
    _reserved_artifact_reason,
    scan,
)


# ---- _reserved_artifact_reason unit tests ----

class TestReservedArtifactReason:
    def test_reserved_filename_top_level(self):
        assert _reserved_artifact_reason(Path("result.json")) == "reserved filename"
        assert _reserved_artifact_reason(Path("prediction.csv")) == "reserved filename"
        assert _reserved_artifact_reason(Path("step_result.json")) == "reserved filename"
        assert _reserved_artifact_reason(Path("trace.json")) == "reserved filename"

    def test_reserved_filename_in_context(self):
        # Reserved names match even when nested.
        assert _reserved_artifact_reason(Path("context/result.json")) == "reserved filename"

    def test_reserved_suffix(self):
        assert _reserved_artifact_reason(Path("creatinine_age_analysis_result.json")) == "reserved suffix"
        assert _reserved_artifact_reason(Path("foo_prediction.csv")) == "reserved suffix"
        assert _reserved_artifact_reason(Path("step_0_results.pkl")) == "reserved suffix"

    def test_reserved_prefix(self):
        assert _reserved_artifact_reason(Path("intermediate_budget_full.pkl")) == "reserved prefix"
        assert _reserved_artifact_reason(Path("step_0_code.py")) == "reserved prefix"

    def test_reserved_directory_anywhere(self):
        # Any path component matching a reserved dir → skip.
        assert _reserved_artifact_reason(Path("output/anything.csv")) == "reserved dir 'output'"
        assert _reserved_artifact_reason(Path("context/output/result.json")) == "reserved dir 'output'"
        assert _reserved_artifact_reason(Path("workspace/steps/step_0_code.py")) == "reserved dir 'workspace'"
        assert _reserved_artifact_reason(Path("temp/step_0_results.pkl")) == "reserved dir 'temp'"

    def test_legitimate_paths_not_skipped(self):
        # All legitimate input paths should return None.
        assert _reserved_artifact_reason(Path("context/csv/events.csv")) is None
        assert _reserved_artifact_reason(Path("context/json/drivers.json")) is None
        assert _reserved_artifact_reason(Path("context/db/results.db")) is None
        assert _reserved_artifact_reason(Path("context/doc/budget.md")) is None
        assert _reserved_artifact_reason(Path("context/knowledge.md")) is None

    def test_filename_not_starting_with_step_underscore(self):
        # 'steps.csv' is NOT a reserved prefix match (prefix is 'step_').
        assert _reserved_artifact_reason(Path("steps.csv")) is None
        # But 'step_results.csv' IS — has 'step_' prefix.
        assert _reserved_artifact_reason(Path("step_results.csv")) == "reserved prefix"


# ---- scan() integration tests with temp dirs ----


def _write(p: Path, content: str = ""):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _make_sqlite(p: Path, populate: bool = True):
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    if populate:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
    else:
        # Force a valid SQLite header to be written (otherwise an opened-but-
        # untouched DB file is 0 bytes, which is our zero-byte case, not the
        # no-tables case we want here).
        conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()


class TestScanBlacklist:
    def test_legitimate_files_kept(self, tmp_path):
        # A minimal legitimate task: task.json, context/csv/x.csv, context/db/y.db.
        _write(tmp_path / "task.json", '{"task_id": "t", "question": "?"}')
        _write(tmp_path / "context/csv/data.csv", "a,b\n1,2\n")
        _make_sqlite(tmp_path / "context/db/db.db")

        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        assert "context/csv/data.csv" in paths
        assert "context/db/db.db" in paths
        assert len(paths) == 2  # task.json itself is always skipped

    def test_skips_top_level_reserved_filenames(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        _write(tmp_path / "context/csv/x.csv", "a\n1\n")
        _write(tmp_path / "result.json", "{}")
        _write(tmp_path / "step_result.json", "{}")
        _write(tmp_path / "prediction.csv", "x\n")
        _write(tmp_path / "trace.json", "{}")
        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        assert paths == {"context/csv/x.csv"}

    def test_skips_reserved_filenames_in_context(self, tmp_path):
        # Files matching reserved names anywhere (including under context/)
        # should still be skipped — they're agent output, even if mislocated.
        _write(tmp_path / "task.json", "{}")
        _write(tmp_path / "context/csv/x.csv", "a\n1\n")
        _write(tmp_path / "context/result.json", "{}")  # leaked into context/
        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        assert "context/csv/x.csv" in paths
        assert "context/result.json" not in paths

    def test_skips_output_dir(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        _write(tmp_path / "context/csv/x.csv", "a\n1\n")
        _write(tmp_path / "output/anything.csv", "x\n")
        _write(tmp_path / "context/output/result.json", "{}")
        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        assert paths == {"context/csv/x.csv"}

    def test_skips_intermediate_prefix(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        _write(tmp_path / "context/csv/x.csv", "a\n1\n")
        _write(tmp_path / "context/intermediate_budget_full.pkl", "")
        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        assert paths == {"context/csv/x.csv"}

    def test_skips_task_specific_result_suffix(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        _write(tmp_path / "context/csv/x.csv", "a\n1\n")
        _write(tmp_path / "creatinine_age_analysis_result.json", "{}")
        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        assert paths == {"context/csv/x.csv"}


class TestScanDotDirs:
    """Profiler must not descend into dot-directories (e.g., self-created
    .dataline_cache or external .git, .ipynb_checkpoints).

    This is the L3.5 defence against the audit-found self-pollution loop:
    B2 cache writes used to land inside task_dir; the NEXT scan would then
    read those JSON files back as "inputs" and feed them to the planner.
    """

    def test_dotdir_contents_skipped(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        _write(tmp_path / "context/csv/x.csv", "a\n1\n")
        # Simulate a cache directory left over from a prior run.
        _write(tmp_path / ".dataline_cache/doc_glossary/abc.json", '{"glossary":{}}')
        _write(tmp_path / ".git/HEAD", "ref: refs/heads/main")
        _write(tmp_path / ".ipynb_checkpoints/x-checkpoint.csv", "a\n1\n")
        manifest = scan(str(tmp_path))
        paths = {Path(e.file_path).relative_to(tmp_path).as_posix() for e in manifest.entries}
        # Only the legitimate input file should remain — nothing under a
        # dot-directory should ever be entered.
        assert paths == {"context/csv/x.csv"}


class TestScanEmptyDb:
    def test_zero_byte_db(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        empty_db = tmp_path / "context/db/empty.db"
        empty_db.parent.mkdir(parents=True, exist_ok=True)
        empty_db.touch()  # zero bytes
        manifest = scan(str(tmp_path))
        entries = [e for e in manifest.entries if Path(e.file_path).name == "empty.db"]
        assert len(entries) == 1
        assert entries[0].summary["empty"] is True
        assert entries[0].summary["empty_reason"] == "zero-bytes"
        assert entries[0].summary["tables"] == []

    def test_sqlite_with_no_tables(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        # Create empty-schema SQLite (file exists, valid SQLite header, no tables)
        _make_sqlite(tmp_path / "context/db/notables.db", populate=False)
        manifest = scan(str(tmp_path))
        entries = [e for e in manifest.entries if Path(e.file_path).name == "notables.db"]
        assert len(entries) == 1
        assert entries[0].summary["empty"] is True
        assert entries[0].summary["empty_reason"] == "sqlite-no-tables"

    def test_populated_sqlite_not_marked_empty(self, tmp_path):
        _write(tmp_path / "task.json", "{}")
        _make_sqlite(tmp_path / "context/db/real.db", populate=True)
        manifest = scan(str(tmp_path))
        entries = [e for e in manifest.entries if Path(e.file_path).name == "real.db"]
        assert len(entries) == 1
        assert entries[0].summary.get("empty") is not True
        assert len(entries[0].summary["tables"]) == 1

    def test_duckdb_empty_detected(self, tmp_path):
        # DuckDB file with no tables — confirm we recognize the format and
        # mark empty rather than producing an unparseable error.
        try:
            import duckdb
        except ImportError:
            pytest.skip("duckdb not installed")
        _write(tmp_path / "task.json", "{}")
        db_path = tmp_path / "context/db/duck.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db_path))
        conn.close()
        manifest = scan(str(tmp_path))
        entries = [e for e in manifest.entries if Path(e.file_path).name == "duck.db"]
        assert len(entries) == 1
        assert entries[0].summary["empty"] is True
        assert entries[0].summary["empty_reason"] == "duckdb-no-tables"
        assert entries[0].file_type == "duckdb"
