"""Stateful in-process Python REPL for multi-step task iteration.

WHY:
Default Sandbox spawns a fresh subprocess per execute(). For tasks where the
agent needs incremental reasoning (read large doc → probe patterns → extract
→ compute), each iteration re-imports modules, re-reads files, re-derives
intermediate state. Wastes API calls and prevents the agent from building
on prior step's output.

DESIGN:
- One instance per task, freshly created at task start
- Persistent globals_ dict accumulates variables / imports across calls
- exception in one call does NOT corrupt globals for the next call
- Cleanup at task end frees the namespace
- Thread-safe: TASK_DIR / TEMP_DIR are passed via thread-local context
  in data_helpers (NOT os.environ) so parallel workers don't contaminate
  each other's task state

CONCURRENCY NOTE (lesson from v44 regression):
os.environ is process-shared. With parallel=10 workers each creating their
own REPL, env vars overwrite cross-task and save_result writes to the wrong
TEMP_DIR. This implementation uses data_helpers.set_task_context() which
binds task_dir / temp_dir to threading.local(). Each worker thread sees
its own context.

USAGE:
    repl = StatefulPythonExec(task_dir="/path", temp_dir="/tmp/x")
    try:
        r1 = repl.execute("import re; text = open('doc.md').read()")
        r2 = repl.execute("matches = re.findall(r'CRE: (\\d+)', text)")
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
    """Best-effort timeout via thread join.

    Cannot truly kill a Python thread. Long-running agent code is bounded
    by orchestrator wall-clock budget at higher level.
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
    """In-process Python REPL with persistent globals across calls."""

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

        self._init_environment()

    def _init_environment(self) -> None:
        """Bind task context for this thread + ensure helpers are importable.

        Uses data_helpers.set_task_context() (thread-local) instead of
        os.environ, so parallel workers don't trample each other.
        """
        # Add helpers and temp_dir to sys.path so `from data_helpers import *` works.
        helpers_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "helpers",
        )
        for p in (self._temp_dir, helpers_path):
            if p and p not in sys.path:
                sys.path.insert(0, p)

        # Set thread-local task context (parallel-safe). Helpers prefer
        # this over os.environ.
        try:
            from dataline.helpers.data_helpers import set_task_context
            set_task_context(self._task_dir, self._temp_dir)
        except ImportError:
            pass

    def execute(self, code: str) -> ReplResult:
        """Run a code block in the persistent globals namespace.

        Re-binds thread-local context per call (cheap, idempotent) so this
        thread always sees its own task_dir / temp_dir even if other threads
        are running concurrently.
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

        # Capture per-task dirs as locals so the inner thread (separate
        # from the caller) can rebind threading.local on its OWN identity.
        bound_task = self._task_dir
        bound_temp = self._temp_dir

        def _run():
            # _TimeoutThread runs in its own thread; threading.local() is
            # per-thread, so this thread starts with empty context. Set it
            # here so helpers see the right dirs from inside agent code.
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
        """Mark closed; clear globals; clear thread-local context."""
        if self._closed:
            return
        self._closed = True
        self.globals_.clear()
        try:
            from dataline.helpers.data_helpers import clear_task_context
            clear_task_context()
        except ImportError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.cleanup()
        return False
