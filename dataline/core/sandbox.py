"""Code execution sandbox with persistent state across steps.

Supports two execution modes:
- Python (.py): full Python scripts via subprocess
- SQL (.sql): raw SQL executed via DuckDB (unified engine for CSV, JSON,
  Parquet, and SQLite .db files) with automatic registration and save_result()
"""

from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .types import SandboxResult


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
    """Execute code in isolated subprocess with persistent temp directory."""

    def __init__(
        self,
        task_dir: str,
        timeout: int = 120,
        max_memory_mb: int = 1024,
    ):
        self._task_dir = os.path.abspath(task_dir)
        self._timeout = timeout
        self._max_memory_mb = max_memory_mb
        self._temp_dir = tempfile.mkdtemp(prefix="dataline_")
        self._step_count = 0
        self._install_helpers()

    @property
    def temp_dir(self) -> str:
        return self._temp_dir

    def execute(self, code: str, step_id: str | None = None, language: str = "python") -> SandboxResult:
        """Execute code. Language auto-detected if not specified.

        Args:
            code: Python script or raw SQL query.
            step_id: Step identifier for file naming.
            language: "python" | "sql" — if "sql", wraps in DuckDB/SQLite runner.

        Returns:
            SandboxResult with stdout, stderr, return_code, structured_json.
        """
        if step_id is None:
            step_id = f"step_{self._step_count}"
        self._step_count += 1

        # Clear previous step_result.json so a failed step never inherits the
        # previous step's structured output.
        result_path = Path(self._temp_dir) / "step_result.json"
        if result_path.exists():
            result_path.unlink()

        # SQL mode: wrap raw SQL in a Python runner script
        if language == "sql":
            code = self._wrap_sql(code)

        # Write code to temp file
        code_path = Path(self._temp_dir) / f"{step_id}.py"
        code_path.write_text(code, encoding="utf-8")

        env = os.environ.copy()
        env["TASK_DIR"] = self._task_dir
        env["TEMP_DIR"] = self._temp_dir
        env["PYTHONIOENCODING"] = "utf-8"

        start = time.time()
        try:
            proc = subprocess.run(
                ["python", str(code_path)],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
                cwd=self._temp_dir,
            )
            elapsed_ms = int((time.time() - start) * 1000)

            return SandboxResult(
                stdout=proc.stdout[:10000],  # cap output size
                stderr=proc.stderr[:5000],
                return_code=proc.returncode,
                execution_time_ms=elapsed_ms,
                step_id=step_id,
                structured_json=self._read_step_result(),
            )
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

    def _wrap_sql(self, sql: str) -> str:
        """Wrap raw SQL in a DuckDB runner script.

        DuckDB handles everything: CSV/JSON/Parquet natively, SQLite via ATTACH.
        """
        escaped_sql = sql.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')

        return _SQL_RUNNER_TEMPLATE.replace("__SQL_PLACEHOLDER__", escaped_sql)

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
