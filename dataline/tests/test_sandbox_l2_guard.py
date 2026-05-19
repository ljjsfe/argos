"""Tests for L2 sandbox-guard: detect + clean up TASK_DIR writes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from dataline.core.sandbox import (
    Sandbox,
    _snapshot_dir,
    _strip_task_dir_writes,
)
from dataline.core.types import SandboxResult


def test_snapshot_empty_dir(tmp_path):
    assert _snapshot_dir(str(tmp_path)) == {}


def test_snapshot_picks_up_files(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("y")
    snap = _snapshot_dir(str(tmp_path))
    assert len(snap) == 2
    assert any(p.endswith("a.txt") for p in snap)
    assert any(p.endswith("b.txt") for p in snap)


def test_strip_task_dir_writes_no_violations(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    pre = _snapshot_dir(str(tmp_path))
    result = SandboxResult(stdout="ok", stderr="", return_code=0, execution_time_ms=10)
    out = _strip_task_dir_writes(result, str(tmp_path), pre)
    assert out.stderr == ""
    assert out.stdout == "ok"


def test_strip_task_dir_writes_detects_new_file(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    pre = _snapshot_dir(str(tmp_path))
    # Simulate agent code writing into TASK_DIR
    leak = tmp_path / "output" / "result.json"
    leak.parent.mkdir(parents=True, exist_ok=True)
    leak.write_text("{}")
    assert leak.exists()
    result = SandboxResult(stdout="done", stderr="", return_code=0, execution_time_ms=10)
    out = _strip_task_dir_writes(result, str(tmp_path), pre)
    assert "L2_LEAK_GUARD" in out.stderr
    assert "output/result.json" in out.stderr
    assert not leak.exists(), "L2 guard should have deleted the leaked file"


def test_strip_task_dir_writes_detects_modified_file(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("a,b\n1,2\n")
    pre = _snapshot_dir(str(tmp_path))
    # Mutate the file
    import time
    time.sleep(0.01)
    f.write_text("a,b\n9,9\n")
    result = SandboxResult(stdout="", stderr="", return_code=0, execution_time_ms=10)
    out = _strip_task_dir_writes(result, str(tmp_path), pre)
    assert "L2_LEAK_GUARD" in out.stderr
    # The modified file is deleted; in practice agent code should not modify
    # input data, so this is the right outcome.
    assert not f.exists()


def test_strip_preserves_existing_stderr(tmp_path):
    pre = _snapshot_dir(str(tmp_path))
    leak = tmp_path / "stray.txt"
    leak.write_text("x")
    result = SandboxResult(
        stdout="", stderr="prior error message", return_code=1, execution_time_ms=10,
    )
    out = _strip_task_dir_writes(result, str(tmp_path), pre)
    assert out.stderr.startswith("prior error message")
    assert "L2_LEAK_GUARD" in out.stderr
    assert out.return_code == 1  # don't mutate other fields


def test_sandbox_execute_cleans_leaked_write(tmp_path):
    """End-to-end: code that writes to TASK_DIR has its output removed."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.json").write_text('{"task_id":"x","question":"?"}')
    sb = Sandbox(task_dir=str(task_dir), timeout=30)
    try:
        leak_path = task_dir / "output" / "result.json"
        code = (
            "import os, json\n"
            f"os.makedirs(r{str(leak_path.parent)!r}, exist_ok=True)\n"
            f"with open(r{str(leak_path)!r}, 'w') as f: json.dump({{'leak':1}}, f)\n"
            "print('wrote')\n"
        )
        result = sb.execute(code, step_id="t1", language="python", use_scratch=False)
        # File should be deleted by the guard.
        assert not leak_path.exists(), "L2 guard should have removed the write"
        # Stderr should carry the violation note.
        assert "L2_LEAK_GUARD" in result.stderr
        assert "output/result.json" in result.stderr
    finally:
        import shutil
        shutil.rmtree(sb.temp_dir, ignore_errors=True)
