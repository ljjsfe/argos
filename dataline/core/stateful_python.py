"""Stateful in-process Python REPL for cross-iteration variable persistence.

DESIGN (v3, after two prior failures):
- One instance per task, freshly created at task start
- Persistent globals_ dict — variables / imports persist across execute()
- Exception in one call does NOT corrupt globals for the next call
- Thread-safe via thread-local task context (data_helpers.set_task_context)
- NO process-global mutations: no os.environ writes, no os.chdir
- Agent code that needs file paths MUST use safe_read_csv et al. (which read
  thread-local TASK_DIR) or absolute paths — relative-to-cwd paths break
  under parallel execution

CONCURRENCY HISTORY:
v1 (v44): used os.environ → parallel workers cross-contaminated → 8/50
v2 (v45): added threading.local for env, but kept os.chdir → cwd race in
          orchestrator's relative path resolution → 35 lucky / 9 unlucky
v3 (this): no global state mutations. Caller (Sandbox) ensures absolute
          paths via main.py defensive abspath().

USAGE:
    repl = StatefulPythonExec(task_dir="/abs/path", temp_dir="/abs/tmp")
    try:
        r1 = repl.execute("import re; text = open('/abs/path/doc.md').read()")
        r2 = repl.execute("print(len(text))")  # text persists
    finally:
        repl.cleanup()
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplResult:
    """Result of a single execute() call."""
    stdout: str
    stderr: str
    return_code: int        # 0 success, 1 exception, -1 timeout
    execution_time_ms: int
    exception_type: str = ""


class _TimeoutThread(threading.Thread):
    """Best-effort timeout via thread join. Cannot truly kill a thread."""

    def __init__(self, target, args=(), kwargs=None):
        super().__init__(daemon=True)
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.result = None
        self.exception = None

    def run(self):
        try:
            self.result = self._target(*self._args, **self._kwargs)
        except BaseException as e:
            self.exception = e


class StatefulPythonExec:
    """In-process Python REPL with persistent globals; no process-global state."""

    def __init__(
        self,
        task_dir: str,
        temp_dir: str,
        timeout: int = 120,
    ):
        self._task_dir = os.path.abspath(task_dir)
        self._temp_dir = os.path.abspath(temp_dir) if temp_dir else ""
        self._timeout = timeout
        self._call_count = 0
        self._closed = False

        self.globals_: dict = {
            "__name__": "__stateful_repl__",
            "__doc__": None,
            "__builtins__": __builtins__,
        }

        # Add temp_dir + helpers to sys.path (idempotent across instances).
        # sys.path is process-shared but only-grow — safe to add the same paths
        # repeatedly across parallel workers.
        helpers_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "helpers",
        )
        for p in (self._temp_dir, helpers_path):
            if p and p not in sys.path:
                sys.path.insert(0, p)

    def execute(self, code: str) -> ReplResult:
        """Run code in persistent globals. Sets thread-local task context for helpers."""
        if self._closed:
            return ReplResult(
                stdout="", stderr="REPL is closed",
                return_code=1, execution_time_ms=0,
                exception_type="RuntimeError",
            )

        self._call_count += 1
        start = time.time()
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        # Capture per-instance dirs in closure so the inner thread sees them.
        bound_task = self._task_dir
        bound_temp = self._temp_dir

        def _run():
            # _TimeoutThread runs in its own thread; threading.local() is
            # per-thread, so this thread starts with empty context. Set it
            # here so helpers (safe_read_csv, save_result) resolve paths
            # against THIS task's dirs, not whatever another worker last wrote.
            try:
                from dataline.helpers.data_helpers import set_task_context
                set_task_context(bound_task, bound_temp)
            except ImportError:
                pass
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                exec(compile(code, f"<repl_step_{self._call_count}>", "exec"),
                     self.globals_)

        thread = _TimeoutThread(_run)
        thread.start()
        thread.join(self._timeout)
        elapsed_ms = int((time.time() - start) * 1000)

        if thread.is_alive():
            return ReplResult(
                stdout=out_buf.getvalue()[:10000],
                stderr=(err_buf.getvalue()
                        + f"\n[REPL] Step exceeded {self._timeout}s timeout")[:5000],
                return_code=-1,
                execution_time_ms=elapsed_ms,
                exception_type="TimeoutError",
            )

        if thread.exception is not None:
            tb = traceback.format_exception(
                type(thread.exception), thread.exception, thread.exception.__traceback__,
            )
            return ReplResult(
                stdout=out_buf.getvalue()[:10000],
                stderr=(err_buf.getvalue() + "".join(tb))[:5000],
                return_code=1,
                execution_time_ms=elapsed_ms,
                exception_type=type(thread.exception).__name__,
            )

        return ReplResult(
            stdout=out_buf.getvalue()[:10000],
            stderr=err_buf.getvalue()[:5000],
            return_code=0,
            execution_time_ms=elapsed_ms,
        )

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.globals_.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False
