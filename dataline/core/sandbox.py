"""Code execution sandbox with persistent state across steps.

Supports two execution modes:
- Python (.py): full Python scripts via subprocess
- SQL (.sql): raw SQL executed via DuckDB (unified engine for CSV, JSON,
  Parquet, and SQLite .db files) with automatic registration and save_result()

Path resolution: a filtered scratch directory is created per task (cached
across iterations) where legitimate input files are materialized via
shutil.copy2 — not symlink/hardlink. Copy is required to (a) prevent
os.readlink leaking the raw task_dir absolute path, and (b) prevent
write-through corruption of the raw input (which hardlinks would allow).
LLM-generated code sees flat filenames in cwd; no need for
os.environ["TASK_DIR"]. See 2026-05-20 audit Findings 1+2 for details.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .types import SandboxResult

logger = logging.getLogger(__name__)


def _snapshot_dir(root: str) -> dict[str, float]:
    """Map path → mtime for every regular file under `root`. Used by L2
    sandbox-guard to detect agent code writing into the (read-only) TASK_DIR.
    """
    snap: dict[str, float] = {}
    if not os.path.isdir(root):
        return snap
    for d, _, files in os.walk(root, followlinks=False):
        for f in files:
            p = os.path.join(d, f)
            try:
                snap[p] = os.stat(p).st_mtime
            except OSError:
                continue
    return snap


def _strip_task_dir_writes(
    result: "SandboxResult", task_dir: str, pre_snapshot: dict[str, float],
) -> "SandboxResult":
    """L2 guard: after every execute(), find files in TASK_DIR that were
    created/modified during the step. Delete them and append a clear
    violation note to stderr so PlannerCoder learns to avoid the pattern.

    Universal hygiene — applies regardless of benchmark / data shape.
    Production submission containers typically mount input read-only, so
    this is defense-in-depth for local + Phase 2 envs alike.
    """
    post = _snapshot_dir(task_dir)
    violations: list[str] = []
    for p, mtime in post.items():
        if p not in pre_snapshot or pre_snapshot[p] < mtime - 1e-6:
            violations.append(p)
    if not violations:
        return result

    # Delete the offending writes so subsequent steps don't see them.
    for p in violations:
        try:
            os.remove(p)
        except OSError as e:
            logger.warning("L2 guard: could not delete leaked write %s: %s", p, e)

    rel_paths = [
        os.path.relpath(p, task_dir) for p in sorted(violations)[:6]
    ]
    note = (
        "\n[SANDBOX:L2_LEAK_GUARD] Your code wrote into TASK_DIR (read-only). "
        f"Files removed: {rel_paths}. "
        "All writes (intermediates, results) MUST go to TEMP_DIR via "
        "save_result() / save_intermediate() / explicit absolute paths under "
        "TEMP_DIR. Re-write this step without TASK_DIR writes."
    )
    # Re-assemble SandboxResult with augmented stderr; preserve other fields.
    return SandboxResult(
        stdout=result.stdout,
        stderr=(result.stderr or "") + note,
        return_code=result.return_code,
        execution_time_ms=result.execution_time_ms,
        step_id=result.step_id,
        structured_json=result.structured_json,
    )


_SQL_RUNNER_TEMPLATE = '''"""Auto-generated SQL runner — DuckDB unified engine."""
import os
import json
import glob

task_dir = os.environ["TASK_DIR"]
temp_dir = os.environ["TEMP_DIR"]

sql_query = """__SQL_PLACEHOLDER__"""

import duckdb
conn = duckdb.connect()
conn.execute("INSTALL sqlite; LOAD sqlite;")

# Register CSV files as views
registered_views = set()
csv_files = glob.glob(os.path.join(task_dir, "**/*.csv"), recursive=True)
for csv_path in csv_files:
    view_name = os.path.splitext(os.path.basename(csv_path))[0]
    view_name = view_name.replace("-", "_").replace(" ", "_")
    try:
        conn.execute(
            f"CREATE OR REPLACE VIEW {view_name} AS "
            f"SELECT * FROM read_csv_auto(\\'{csv_path}\\')"
        )
        registered_views.add(view_name.lower())
    except Exception as e:
        print(f"Warning: could not register {csv_path}: {e}")

# Register JSON files as views (KDD format: {"table": "name", "records": [...]})
json_files = glob.glob(os.path.join(task_dir, "**/*.json"), recursive=True)
for json_path in json_files:
    view_name = os.path.splitext(os.path.basename(json_path))[0]
    view_name = view_name.replace("-", "_").replace(" ", "_")
    if view_name.lower() in registered_views:
        continue  # CSV view takes precedence
    try:
        with open(json_path, "r") as f:
            jdata = json.load(f)
        # KDD format: {"table": "...", "records": [...]}
        if isinstance(jdata, dict) and "records" in jdata:
            records = jdata["records"]
        elif isinstance(jdata, list):
            records = jdata
        else:
            continue
        if not records:
            continue
        import pandas as _pd
        df_json = _pd.DataFrame(records)
        # JSON often stores numeric IDs as strings. Coerce safe numeric-looking
        # columns so cross-format joins with SQLite integer keys work. Preserve
        # code-like values with leading zeroes (e.g. "00123").
        for _col in list(df_json.columns):
            try:
                _series = df_json[_col]
                if _series.dtype != object:
                    continue
                _nonnull = _series.dropna().astype(str).str.strip()
                if _nonnull.empty:
                    continue
                _has_leading_zero = _nonnull.str.match(r"^0\\d+").any()
                _numeric_like = _nonnull.str.match(r"^-?\\d+(?:\\.\\d+)?$").mean()
                _id_like = any(
                    token in _col.lower()
                    for token in ("id", "key", "count", "score", "amount", "number")
                )
                if _id_like and not _has_leading_zero and _numeric_like >= 0.9:
                    df_json[_col] = _pd.to_numeric(df_json[_col], errors="coerce")
            except Exception:
                pass
        conn.register(view_name, df_json)
        registered_views.add(view_name.lower())
    except Exception as e:
        print(f"Warning: could not register JSON {json_path}: {e}")

# Attach SQLite databases and expose tables as top-level views
db_files = glob.glob(os.path.join(task_dir, "**/*.db"), recursive=True) + \\
           glob.glob(os.path.join(task_dir, "**/*.sqlite"), recursive=True)
db_files = [p for p in db_files if os.path.getsize(p) > 0]  # skip 0-byte stubs
for db_path in db_files:
    alias = os.path.splitext(os.path.basename(db_path))[0]
    try:
        conn.execute(f"ATTACH \\'{db_path}\\' AS {alias} (TYPE sqlite)")
        # Expose SQLite tables as top-level views so SQL can use simple names.
        # Skip if a CSV view with the same name already exists — the CSV is a
        # separate data source. The SQLite table is still reachable via alias.table.
        for row in conn.execute("SHOW ALL TABLES").fetchall():
            if row[0] == alias and row[2] != "sqlite_sequence":
                tbl = row[2]
                if tbl.lower() in registered_views:
                    continue  # CSV view takes precedence
                try:
                    conn.execute(f"CREATE OR REPLACE VIEW {tbl} AS SELECT * FROM {alias}.{tbl}")
                    registered_views.add(tbl.lower())
                except Exception:
                    pass
    except Exception as e:
        print(f"Warning: could not attach {db_path}: {e}")

df = conn.execute(sql_query).fetchdf()
result = {col: list(df[col]) for col in df.columns}
conn.close()

if result:
    import pandas as pd
    df_out = pd.DataFrame(result)
    print(df_out.to_string(index=False))
    print(f"\\nResult rows: {len(next(iter(result.values())))}")
else:
    print("(empty result)")

from data_helpers import save_result
save_result(answer=result, row_counts={"result_rows": len(next(iter(result.values()), []))})
'''


class Sandbox:
    """Execute code in isolated subprocess with persistent temp directory.

    Optionally accepts a StatefulPythonExec; when present, Python iterations
    run in-process with persistent globals (variables / imports survive
    across iterations of the SAME task). SQL still uses subprocess.
    """

    def __init__(
        self,
        task_dir: str,
        timeout: int = 120,
        max_memory_mb: int = 1024,
        stateful_repl: object | None = None,
    ):
        self._task_dir = os.path.abspath(task_dir)
        self._timeout = timeout
        self._max_memory_mb = max_memory_mb
        self._temp_dir = tempfile.mkdtemp(prefix="dataline_")
        self._step_count = 0
        self._stateful_repl = stateful_repl
        # Extra CSV paths to symlink into scratch dir on first build.
        # Set by orchestrator after Profiler doc-extraction step (Block 4).
        # Each tuple is (link_basename, absolute_csv_path) so the link can
        # use a friendly DuckDB view name regardless of the cache filename.
        self.extra_csv_links: list[tuple[str, str]] = []
        # Filtered task-view: lazy-built by _build_scratch and reused across
        # all execute() calls. CRITICAL: this is the path passed to subprocess
        # as TASK_DIR (and to StatefulPythonExec) so that SQL wrapper + Python
        # helpers + Python cwd all see the SAME filtered view of the input.
        # The original self._task_dir is still used for the L2 leak-guard
        # snapshot (so writes back to the real input are still detected).
        # See 2026-05-20 audit Finding 1/2/3.
        self._scratch_dir: str | None = None
        self._install_helpers()

    @property
    def temp_dir(self) -> str:
        return self._temp_dir

    def execute(
        self, code: str, step_id: str | None = None,
        language: str = "python", use_scratch: bool = True,
    ) -> SandboxResult:
        """Execute code in sandbox.

        Args:
            code: Python script or raw SQL query.
            step_id: Step identifier for file naming.
            language: "python" | "sql" — if "sql", wraps in DuckDB/SQLite runner.
            use_scratch: If True, run in filtered scratch dir (copies of
                task files) so code can use relative paths. Set False for
                Analyzer steps that should not be affected by cwd changes.

        Returns:
            SandboxResult with stdout, stderr, return_code, structured_json.
        """
        if step_id is None:
            step_id = f"step_{self._step_count}"
        self._step_count += 1

        # L2 leak prevention: snapshot TASK_DIR before exec. After exec, any
        # new/modified files in TASK_DIR are agent-output leaks and get cleaned
        # up with a violation message attached to stderr.
        pre_snapshot = _snapshot_dir(self._task_dir)

        # Snapshot prior step_result.json so a clean step that ran verification
        # code without re-calling save_result() can carry the prior answer
        # forward (avoids missing_save_result loops). The prior is only
        # restored at the end of THIS step when rc=0 AND no new save_result
        # was written — so a failed step still cannot inherit stale output.
        result_path = Path(self._temp_dir) / "step_result.json"
        prior_step_json = ""
        if result_path.exists():
            try:
                prior_step_json = result_path.read_text(encoding="utf-8")
            except OSError:
                prior_step_json = ""
            result_path.unlink()

        # SQL mode: wrap raw SQL in a Python runner script
        if language == "sql":
            code = self._wrap_sql(code)

        # Determine cwd: scratch dir (symlinks) or temp dir (legacy)
        if use_scratch:
            run_dir = self._build_scratch()
        else:
            run_dir = self._temp_dir

        # Pick the TASK_DIR to expose to user code: filtered scratch view
        # (default — closes audit Finding 1/2), or the raw task_dir as the
        # legacy fallback when scratch is disabled. Critical for SQL wrapper
        # + helpers + Python cwd to share one filtered surface.
        runtime_task_dir = self._scratch_dir if (use_scratch and self._scratch_dir) else self._task_dir

        # Stateful REPL path: in-process Python with persistent globals.
        # No os.chdir — process-global cwd would race other workers. Helpers
        # use thread-local TASK_DIR (set inside REPL.execute) for path
        # resolution. _build_scratch above rebinds repl._task_dir → scratch
        # so the set_task_context call inside REPL points at the filtered view.
        if self._stateful_repl is not None and language == "python":
            result = self._execute_via_repl(code, step_id, prior_step_json)
            return _strip_task_dir_writes(result, self._task_dir, pre_snapshot)

        # Write code to temp file (in TEMP_DIR, not scratch — keeps scratch clean)
        code_path = Path(self._temp_dir) / f"{step_id}.py"
        code_path.write_text(code, encoding="utf-8")

        env = os.environ.copy()
        env["TASK_DIR"] = runtime_task_dir
        env["TEMP_DIR"] = self._temp_dir
        env["PYTHONIOENCODING"] = "utf-8"
        # Add TEMP_DIR to PYTHONPATH so `import data_helpers` works from any cwd
        env["PYTHONPATH"] = self._temp_dir + os.pathsep + env.get("PYTHONPATH", "")

        start = time.time()
        try:
            proc = subprocess.run(
                ["python", str(code_path)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
                cwd=run_dir,
            )
            elapsed_ms = int((time.time() - start) * 1000)

            result = SandboxResult(
                stdout=proc.stdout[:10000],  # cap output size
                stderr=proc.stderr[:5000],
                return_code=proc.returncode,
                execution_time_ms=elapsed_ms,
                step_id=step_id,
                structured_json=self._resolve_structured_json(
                    proc.returncode, prior_step_json,
                ),
            )
            return _strip_task_dir_writes(result, self._task_dir, pre_snapshot)
        except subprocess.TimeoutExpired:
            elapsed_ms = int((time.time() - start) * 1000)
            return SandboxResult(
                stdout="",
                stderr=f"Timeout: execution exceeded {self._timeout}s limit",
                return_code=-1,
                execution_time_ms=elapsed_ms,
                step_id=step_id,
            )
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            return SandboxResult(
                stdout="",
                stderr=f"Sandbox error: {e}",
                return_code=-2,
                execution_time_ms=elapsed_ms,
                step_id=step_id,
            )
        finally:
            # NOTE: scratch is now memoized per-Sandbox-instance (single dir
            # reused across execute() calls). Do NOT delete it here — the
            # 2026-05-20 FilteredTaskView refactor relies on the scratch
            # persisting across iterations. It's cleaned up automatically
            # when _temp_dir is removed at process / Sandbox tear-down.
            pass

    def _execute_via_repl(
        self, code: str, step_id: str, prior_step_json: str = "",
    ) -> SandboxResult:
        """Run Python via the persistent in-process REPL.

        NO os.chdir — global cwd would race parallel workers. REPL sets
        thread-local TASK_DIR/TEMP_DIR for helpers; agent code must use
        absolute paths or our safe_read_* helpers, NOT raw relative paths.
        """
        r = self._stateful_repl.execute(code)
        return SandboxResult(
            stdout=r.stdout,
            stderr=r.stderr,
            return_code=r.return_code,
            execution_time_ms=r.execution_time_ms,
            step_id=step_id,
            structured_json=self._resolve_structured_json(
                r.return_code, prior_step_json,
            ),
        )

    def _resolve_structured_json(
        self, return_code: int, prior_step_json: str,
    ) -> str:
        """Pick the structured_json for this step.

        Carries the prior step's saved answer forward when current step ran
        cleanly (rc=0) but did not call save_result() — avoids
        missing_save_result loops on verification-only follow-up steps.
        rc!=0 always returns whatever the step itself wrote (empty if
        nothing) so failed steps cannot inherit stale answers.
        """
        current = self._read_step_result()
        if current:
            return current
        if return_code == 0 and prior_step_json:
            return prior_step_json
        return ""

    def _wrap_sql(self, sql: str) -> str:
        """Wrap raw SQL in a DuckDB runner script.

        DuckDB handles everything: CSV/JSON/Parquet natively, SQLite via ATTACH.
        """
        escaped_sql = sql.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')

        return _SQL_RUNNER_TEMPLATE.replace("__SQL_PLACEHOLDER__", escaped_sql)

    def _materialize(self, src: str, dst: str) -> bool:
        """Copy `src` to `dst` so scratch is fully isolated from raw input.

        Why copy and not hardlink: an earlier attempt used `os.link()`
        (zero copy cost, AND `os.readlink(dst)` raises EINVAL so the
        original absolute path can't be recovered). BUT hardlinks share
        the inode — if the agent does `open('data.csv', 'w').write(...)`
        the raw task_dir file gets overwritten, and the L2 leak-guard
        then DELETES it as "agent wrote to TASK_DIR". Verified repro:
        raw `data.csv` removed mid-task. (2026-05-20 audit follow-up.)

        Copy is the safe answer:
          - Agent writes go to scratch only; raw input is untouched.
          - `os.readlink(dst)` still raises EINVAL (dst is a regular
            file, not a symlink) so the path-leak from the previous
            audit Finding 1 is still closed.
          - Cost is a one-time hit at task start (~MB/s × file sizes);
            scratch is cached across iterations so we pay once per task.

        Returns True on success, False on any OSError (caller skips that
        entry).
        """
        try:
            shutil.copy2(src, dst)
            return True
        except OSError as e:
            logger.debug("Could not copy %s → %s: %s", src, dst, e)
            return False

    def _sync_extra_csv_links(self, scratch: str) -> None:
        """Materialize Block 4 extracted CSVs into scratch.

        Idempotent — safe to call repeatedly. This is intentionally split
        out of the cached scratch build so late-added entries to
        self.extra_csv_links (set by orchestrator AFTER Sandbox creation)
        still propagate on every _build_scratch() call. (2026-05-20 audit
        Finding 2 fix.)
        """
        for link_basename, csv_path in self.extra_csv_links:
            link = Path(scratch) / f"{link_basename}.csv"
            if link.exists():
                continue
            if not Path(csv_path).is_file():
                logger.debug(
                    "extra_csv missing: %s (link %s)", csv_path, link_basename,
                )
                continue
            self._materialize(csv_path, str(link))

    def _build_scratch(self) -> str:
        """Build (or return cached) filtered task-view directory.

        The scratch dir is a flat directory of materialized copies of
        legitimate task inputs (anything Profiler would scan) plus Block 4
        extracted CSVs. Reserved artifacts (gold.csv, prediction.csv,
        output/, .dataline_cache/, etc.) are excluded by the same hygiene
        rules Profiler uses — a single source of truth.

        Cached per-Sandbox-instance: input tree is built once on first
        call; extra_csv_links are re-synced on every call (cheap, idempotent).
        This is what gets passed to subprocess as TASK_DIR and to
        StatefulPythonExec, so SQL wrapper + Python helpers + Python cwd
        all see the SAME filtered view (closes 2026-05-20 audit Finding
        1/2/3 leakage paths).

        Files are COPIED (not symlinked or hardlinked) so (a) `os.readlink`
        cannot recover the original absolute path, and (b) agent writes
        to scratch don't corrupt the raw input via shared inode (which
        hardlinks would allow, triggering L2 to delete the original).

        The original self._task_dir is NEVER passed to user code; it's only
        used for L2 leak-guard snapshot/restore.
        """
        # Cache hit: still re-sync extra_csv_links in case orchestrator
        # added more between iterations (Finding 2 timing assumption fix).
        if self._scratch_dir is not None:
            self._sync_extra_csv_links(self._scratch_dir)
            return self._scratch_dir

        scratch = tempfile.mkdtemp(prefix="scratch_", dir=self._temp_dir)
        task_path = Path(self._task_dir)

        # Reuse the Profiler's hygiene filter so scratch never exposes agent
        # outputs (result.json / prediction.csv / output/) or self-injected
        # caches (.dataline_cache/).
        #
        # Deep tree-walk + per-file symlink (not per-top-level): glob()
        # follows directory symlinks, so a shallow `os.symlink(context, ...)`
        # would leak anything under context/ regardless of filename — e.g.
        # `context/ground_truth.csv` would appear in `glob(scratch/**/*.csv)`.
        # Walk every leaf and apply the hygiene filter individually instead.
        # (2026-05-20 audit Finding 1 fix.)
        from ..profiler.manifest import (
            RESERVED_DIR_BASENAMES,
            _reserved_artifact_reason,
        )
        for root, dirs, files in os.walk(task_path, followlinks=False):
            rel_root = Path(root).relative_to(task_path)
            # Prune reserved + dot-prefixed subdirs in-place so os.walk skips them.
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in RESERVED_DIR_BASENAMES
            ]
            # Create matching directory under scratch (lazy — only if a file inside passes)
            scratch_sub = (Path(scratch) / rel_root) if rel_root != Path(".") else Path(scratch)
            for fname in files:
                if fname.startswith("."):
                    continue
                if fname == "task.json":
                    # Same Profiler convention — task metadata not exposed as data.
                    continue
                rel_path = rel_root / fname if rel_root != Path(".") else Path(fname)
                if _reserved_artifact_reason(rel_path) is not None:
                    continue
                try:
                    scratch_sub.mkdir(parents=True, exist_ok=True)
                except OSError:
                    continue
                link = scratch_sub / fname
                if link.exists():
                    continue
                self._materialize(os.path.join(root, fname), str(link))

        # data_helpers must be available for `import data_helpers` from cwd.
        # Hardlink for consistency (no path-leak risk; symlink would also be
        # fine since this is our own file, but uniform = simpler).
        helpers = Path(self._temp_dir) / "data_helpers.py"
        if helpers.exists():
            link = Path(scratch) / "data_helpers.py"
            if not link.exists():
                self._materialize(str(helpers), str(link))

        # Block 4: extracted CSVs (delegated to _sync helper for re-use
        # across cache hits — Finding 2 fix).
        self._sync_extra_csv_links(scratch)

        # Symlink step_result.json location so save_result() writes to TEMP_DIR
        # (data_helpers reads TEMP_DIR env var, so this is not needed here)

        # Cache + rebind StatefulPythonExec so its set_task_context uses the
        # filtered scratch view, not the raw task_dir. Same closure for SQL
        # subprocess via env["TASK_DIR"] (see execute()).
        self._scratch_dir = scratch
        if self._stateful_repl is not None:
            try:
                self._stateful_repl._task_dir = scratch
            except AttributeError:
                pass  # foreign REPL impl — leave alone

        return scratch

    def _install_helpers(self) -> None:
        """Copy data_helpers.py to TEMP_DIR so generated code can import it."""
        helpers_src = Path(__file__).parent.parent / "helpers" / "data_helpers.py"
        if helpers_src.exists():
            helpers_dst = Path(self._temp_dir) / "data_helpers.py"
            shutil.copy2(str(helpers_src), str(helpers_dst))

    def _read_step_result(self) -> str:
        """Read step_result.json written by save_result() helper. Returns JSON string or ''."""
        path = Path(self._temp_dir) / "step_result.json"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def save_step_result(self, step_id: str, data: object) -> str:
        """Save step result as pickle for later steps to use."""
        path = Path(self._temp_dir) / f"{step_id}_result.pkl"
        with open(path, "wb") as f:
            pickle.dump(data, f)
        return str(path)

    def cleanup(self) -> None:
        """Remove temp directory."""
        if os.path.exists(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __del__(self) -> None:
        self.cleanup()
