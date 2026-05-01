"""Stateful in-process Python REPL for multi-step task iteration.

WHY THIS EXISTS:
The default Sandbox spawns a fresh subprocess per execute() call. For tasks
where the agent needs incremental reasoning (read large doc, then probe
patterns, then extract, then compute), each iteration re-imports modules,
re-reads files, and re-derives intermediate state. Wastes API calls and
prevents the agent from building on prior step's output.

DESIGN:
- One instance per task, freshly created at task start
- Persistent `globals_` dict accumulates variables / imports across calls
- `execute(code)` runs in self.globals_ with stdout capture and timeout
- Exceptions are caught and returned in result; globals stay valid
- Cleanup at task end prevents memory leak across tasks

TRUST MODEL:
LLM-generated code is already executed inside our process boundary via
subprocess. In-process exec() with timeout/exception guards is equivalent
in trust level (we trust the agent), much faster (no subprocess fork +
re-import per call), and lets state actually persist.

USAGE:
    repl = StatefulPythonExec(task_dir="/path/to/task", temp_dir="/tmp/x")
    try:
        r1 = repl.execute("import re; text = open('doc.md').read()")
        r2 = repl.execute("matches = re.findall(r'CRE: (\\d+)', text)")
        r3 = repl.execute("print(len(matches))")
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
    """Result of a single execute() call against the stateful REPL."""
    stdout: str
    stderr: str
    return_code: int        # 0 success, 1 exception, -1 timeout
    execution_time_ms: int
    exception_type: str = ""    # e.g. "ValueError" if exception raised


class _TimeoutThread(threading.Thread):
    """Best-effort timeout enforcement via thread.

    Cannot truly kill a running thread in CPython (no abort primitive).
    We rely on the LLM-generated code being well-behaved most of the time;
    for genuinely infinite loops, the per-task wall-clock budget at the
    orchestrator level still bounds runtime.
    """

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
    """In-process Python interpreter that persists state across execute() calls."""

    def __init__(
        self,
        task_dir: str,
        temp_dir: str,
        scratch_dir: str | None = None,
        timeout: int = 120,
    ):
        self._task_dir = os.path.abspath(task_dir)
        self._temp_dir = os.path.abspath(temp_dir)
        self._scratch_dir = os.path.abspath(scratch_dir) if scratch_dir else None
        self._timeout = timeout
        self._call_count = 0
        self._closed = False

        self.globals_: dict = {
            "__name__": "__stateful_repl__",
            "__doc__": None,
            "__builtins__": __builtins__,
        }

        self._init_environment()

    def _init_environment(self) -> None:
        """Set up environment vars + working dir + helpers in the persistent globals.

        Mirrors the subprocess-mode Sandbox setup so agent code sees the same
        TASK_DIR, TEMP_DIR, data_helpers import behavior, and DuckDB views.
        """
        os.environ["TASK_DIR"] = self._task_dir
        os.environ["TEMP_DIR"] = self._temp_dir
        os.environ["PYTHONIOENCODING"] = "utf-8"

        # Add temp_dir + helpers root to sys.path so `from data_helpers import *`
        # works inside the REPL the same way it does in subprocess.
        helpers_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "helpers",
        )
        for p in (self._temp_dir, helpers_path):
            if p not in sys.path:
                sys.path.insert(0, p)

        if self._scratch_dir and os.path.isdir(self._scratch_dir):
            try:
                os.chdir(self._scratch_dir)
            except OSError:
                pass

    def execute(self, code: str) -> ReplResult:
        """Run a code block in the persistent globals namespace.

        Returns a ReplResult with captured stdout/stderr and exception info.
        On exception, globals remain intact (one bad step does not corrupt
        the persistent state for the next call).
        """
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

        def _run():
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
                stderr=(err_buf.getvalue() + f"\n[REPL] Step exceeded {self._timeout}s timeout"
                        )[:5000],
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

    def reset(self) -> None:
        """Wipe persistent state without disposing the instance.

        Use between distinct tasks if reusing the same instance (e.g. test
        harness). Production code should create a fresh instance per task.
        """
        self.globals_.clear()
        self.globals_.update({
            "__name__": "__stateful_repl__",
            "__doc__": None,
            "__builtins__": __builtins__,
        })
        self._call_count = 0

    def cleanup(self) -> None:
        """Mark the REPL closed and free the persistent namespace.

        Idempotent. Future execute() calls will return an error.
        """
        if self._closed:
            return
        self._closed = True
        self.globals_.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False
