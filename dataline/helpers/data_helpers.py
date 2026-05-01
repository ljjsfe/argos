"""Sandbox helper functions — automatically available in generated code.

These functions reduce LLM token waste on boilerplate data loading,
type detection, and common transformations. Copied to TEMP_DIR at
sandbox init so generated code can `from data_helpers import *`.

All functions are deterministic, self-contained, and import only
standard library + pandas + numpy.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# --- Thread-local task context (parallel-safe) ---
# When in-process REPL is active with parallel workers, os.environ becomes
# a contention point. threading.local() gives each worker its own context;
# helpers prefer it over env. Subprocess execution is unaffected (each
# subprocess has its own env naturally).

_context = threading.local()


def set_task_context(task_dir: str, temp_dir: str) -> None:
    """Bind task_dir / temp_dir for the current thread."""
    _context.task_dir = os.path.abspath(task_dir) if task_dir else None
    _context.temp_dir = os.path.abspath(temp_dir) if temp_dir else None


def clear_task_context() -> None:
    """Drop the current thread's task context."""
    for attr in ("task_dir", "temp_dir"):
        if hasattr(_context, attr):
            delattr(_context, attr)


def _ctx_task_dir() -> str | None:
    return getattr(_context, "task_dir", None)


def _ctx_temp_dir() -> str | None:
    return getattr(_context, "temp_dir", None)


# --- File loading ---


def safe_read_csv(
    filename: str,
    task_dir: str | None = None,
    **kwargs: Any,
) -> pd.DataFrame:
    """Read CSV with automatic encoding fallback and path resolution.

    Args:
        filename: File name (resolved relative to TASK_DIR) or absolute path.
        task_dir: Override for TASK_DIR env var.
        **kwargs: Passed to pd.read_csv.

    Returns:
        DataFrame with the CSV contents.
    """
    path = _resolve_path(filename, task_dir)
    for encoding in ("utf-8", "latin-1", "cp1252"):
        try:
            return pd.read_csv(path, encoding=encoding, **kwargs)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, encoding="latin-1", errors="replace", **kwargs)


def safe_read_json(
    filename: str,
    task_dir: str | None = None,
) -> Any:
    """Read JSON file with path resolution.

    Returns parsed JSON (dict, list, etc.).
    """
    path = _resolve_path(filename, task_dir)
    for encoding in ("utf-8", "latin-1"):
        try:
            with open(path, encoding=encoding) as f:
                return json.load(f)
        except UnicodeDecodeError:
            continue
    with open(path, encoding="utf-8", errors="replace") as f:
        return json.load(f)


def safe_read_json_df(
    filename: str,
    task_dir: str | None = None,
) -> pd.DataFrame:
    """Read JSON file and return a DataFrame.

    Auto-unwraps nested ``{"table": "...", "records": [...]}`` structures.
    Falls back to ``pd.DataFrame(data)`` for flat list-of-dicts JSON.
    """
    data = safe_read_json(filename, task_dir)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return pd.DataFrame(data)
    if isinstance(data, dict):
        record_arrays = [
            (k, v) for k, v in data.items()
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict)
        ]
        if len(record_arrays) == 1:
            return pd.DataFrame(record_arrays[0][1])
    return pd.DataFrame(data)


def safe_read_excel(
    filename: str,
    task_dir: str | None = None,
    sheet_name: str | int = 0,
    **kwargs: Any,
) -> pd.DataFrame:
    """Read Excel file with path resolution.

    Args:
        filename: File name or absolute path.
        task_dir: Override for TASK_DIR env var.
        sheet_name: Sheet to read (default: first sheet).
        **kwargs: Passed to pd.read_excel.
    """
    path = _resolve_path(filename, task_dir)
    return pd.read_excel(path, sheet_name=sheet_name, **kwargs)


# --- Data structure inspection ---


def describe_data(obj: object, name: str = "data", max_items: int = 5) -> str:
    """Print human-readable structure description of any data object.

    Use this FIRST when loading a new data source to understand its format.
    Robust against mixed-type columns, nested JSON, list-valued fields.

    Args:
        obj: Any Python object (list, dict, DataFrame, Series, scalar, etc.)
        name: Display name for the object
        max_items: Maximum sample items to show per field

    Returns:
        Formatted string describing the structure (also prints it).
    """
    lines: list[str] = []

    try:
        if isinstance(obj, pd.DataFrame):
            lines.append(f"{name}: DataFrame ({obj.shape[0]:,} rows x {obj.shape[1]} cols)")
            for col in obj.columns:
                try:
                    dtype = obj[col].dtype
                    null_pct = obj[col].isna().mean() * 100
                    extra = f", {null_pct:.0f}% null" if null_pct > 0 else ""
                    # nunique can fail on unhashable types (list, dict values)
                    try:
                        nunique = obj[col].nunique()
                    except TypeError:
                        nunique = "?"
                    # Safe sample: repr each value to handle any type
                    sample_vals = obj[col].dropna().head(3)
                    sample = [_safe_repr(v) for v in sample_vals]
                    lines.append(f"  {col}: {dtype} ({nunique} unique{extra}) samples={sample}")
                except Exception as e:
                    lines.append(f"  {col}: (inspect error: {type(e).__name__}: {e})")

        elif isinstance(obj, pd.Series):
            lines.append(f"{name}: Series ({len(obj):,} items, dtype={obj.dtype})")
            try:
                lines.append(f"  unique={obj.nunique()}, null={obj.isna().sum()}")
            except TypeError:
                lines.append(f"  unique=?, null={obj.isna().sum()}")
            sample = [_safe_repr(v) for v in obj.dropna().head(max_items)]
            lines.append(f"  samples={sample}")

        elif isinstance(obj, list):
            lines.append(f"{name}: list ({len(obj):,} items)")
            if len(obj) == 0:
                lines.append("  (empty)")
            elif isinstance(obj[0], dict):
                # Collect keys from first few items (not just first — keys may vary)
                all_keys: dict[str, int] = {}
                for item in obj[:20]:
                    if isinstance(item, dict):
                        for k in item:
                            all_keys[k] = all_keys.get(k, 0) + 1
                keys = list(all_keys.keys())
                lines.append(f"  Each item is a dict with up to {len(keys)} keys: {keys[:30]}")
                if len(keys) > 30:
                    lines.append(f"  ... and {len(keys) - 30} more keys")
                for key in keys[:15]:
                    try:
                        values = [item.get(key) for item in obj[:min(50, len(obj))] if isinstance(item, dict)]
                        types = set(type(v).__name__ for v in values if v is not None)
                        type_str = "/".join(sorted(types)) if types else "null"
                        null_count = sum(1 for v in values if v is None)
                        if any(isinstance(v, (list, dict)) for v in values):
                            # Nested structure — show type and size, not content
                            nested_sizes = [len(v) for v in values if isinstance(v, (list, dict))]
                            avg_size = sum(nested_sizes) / max(len(nested_sizes), 1)
                            lines.append(f"  {key}: {type_str} (nested, avg size={avg_size:.0f}) null={null_count}")
                        else:
                            non_null = [v for v in values if v is not None]
                            unique_vals = set(_safe_repr(v) for v in non_null[:50])
                            if len(unique_vals) <= max_items:
                                lines.append(f"  {key}: {type_str} values={sorted(unique_vals)} null={null_count}")
                            else:
                                sample = [_safe_repr(v) for v in non_null[:3]]
                                lines.append(f"  {key}: {type_str} ({len(unique_vals)}+ unique) samples={sample} null={null_count}")
                    except Exception as e:
                        lines.append(f"  {key}: (inspect error: {type(e).__name__})")
            else:
                sample = [_safe_repr(v) for v in obj[:max_items]]
                types = set(type(v).__name__ for v in obj[:50])
                lines.append(f"  item types: {'/'.join(sorted(types))}")
                lines.append(f"  samples: {sample}")

        elif isinstance(obj, dict):
            lines.append(f"{name}: dict ({len(obj)} keys)")
            for key in list(obj.keys())[:15]:
                try:
                    val = obj[key]
                    val_type = type(val).__name__
                    if isinstance(val, (list, dict)):
                        val_preview = f"{val_type}({len(val)} items)"
                    elif isinstance(val, str) and len(val) > 50:
                        val_preview = f"str({len(val)} chars): '{val[:50]}...'"
                    else:
                        val_preview = _safe_repr(val)
                    lines.append(f"  {key}: {val_preview}")
                except Exception:
                    lines.append(f"  {key}: (inspect error)")

        else:
            lines.append(f"{name}: {type(obj).__name__} = {repr(obj)[:200]}")

    except Exception as e:
        lines.append(f"{name}: (describe_data failed: {type(e).__name__}: {e})")

    result = "\n".join(lines)
    print(result)
    return result


def _safe_repr(val: object, max_len: int = 80) -> str:
    """Safe repr that never raises and caps length."""
    try:
        r = repr(val)
        if len(r) > max_len:
            return r[:max_len] + "..."
        return r
    except Exception:
        return f"<{type(val).__name__}>"


# --- DataFrame inspection ---


def describe_df(df: pd.DataFrame, name: str = "df") -> str:
    """Produce a compact summary of a DataFrame for printing.

    Includes: shape, dtypes, null counts, and first 3 rows.
    Robust against mixed-type columns and wide DataFrames.
    """
    try:
        lines = [
            f"=== {name}: {df.shape[0]} rows × {df.shape[1]} cols ===",
            "",
            "Columns:",
        ]
        for col in df.columns:
            try:
                dtype = df[col].dtype
                nulls = df[col].isna().sum()
                try:
                    nunique = df[col].nunique()
                except TypeError:
                    nunique = "?"
                null_info = f", {nulls} nulls" if nulls > 0 else ""
                lines.append(f"  {col} ({dtype}, {nunique} unique{null_info})")
            except Exception as e:
                lines.append(f"  {col} (inspect error: {type(e).__name__})")

        # Cap head output for wide or long-valued DataFrames
        try:
            head_str = df.head(3).to_string(max_colwidth=60)
            if len(head_str) > 3000:
                head_str = head_str[:3000] + "\n... (truncated)"
            lines.append(f"\nFirst 3 rows:\n{head_str}")
        except Exception:
            lines.append("\nFirst 3 rows: (display error)")

        return "\n".join(lines)
    except Exception as e:
        return f"=== {name}: describe_df failed: {type(e).__name__}: {e} ==="


# --- Column detection ---


def find_join_keys(df_a: pd.DataFrame, df_b: pd.DataFrame) -> list[str]:
    """Find columns shared by name between two DataFrames.

    Returns column names that exist in both (case-insensitive match).
    """
    cols_a = {c.lower(): c for c in df_a.columns}
    cols_b = {c.lower(): c for c in df_b.columns}
    shared = set(cols_a.keys()) & set(cols_b.keys())
    return sorted(cols_a[k] for k in shared)


def detect_date_columns(df: pd.DataFrame) -> list[str]:
    """Detect columns that look like dates (string or datetime).

    Returns list of column names that are likely date/datetime.
    """
    date_patterns = [
        re.compile(r"^\d{4}-\d{2}-\d{2}"),
        re.compile(r"^\d{2}/\d{2}/\d{4}"),
        re.compile(r"^\d{4}/\d{2}/\d{2}"),
    ]
    result: list[str] = []
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            result.append(str(col))
            continue
        if df[col].dtype == object:
            sample = df[col].dropna().head(10)
            if len(sample) >= 3:
                matches = sum(
                    1 for v in sample
                    if isinstance(v, str) and any(p.match(v) for p in date_patterns)
                )
                if matches / len(sample) > 0.5:
                    result.append(str(col))
    return result


def clean_numeric(series: pd.Series) -> pd.Series:
    """Convert string series with currency/percentage markers to numeric.

    Handles: $1,234.56, €100, 45.6%, 1,000, etc.
    Returns numeric Series (non-convertible values become NaN).
    """
    if pd.api.types.is_numeric_dtype(series):
        return series

    cleaned = (
        series.astype(str)
        .str.strip()
        .str.replace(r"^[\$€£¥]", "", regex=True)
        .str.replace(r"%$", "", regex=True)
        .str.replace(",", "", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce")


# --- Multimodal readers (Phase 2 ready) ---


def safe_read_text(filename: str, task_dir: str | None = None) -> str:
    """Read any plain-text file (markdown, txt, log) into a string.

    Use this for narrative markdown / docs whose content the agent needs to
    parse with regex or string ops. Returns the full text.
    """
    path = _resolve_path(filename, task_dir)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def safe_read_pdf(filename: str, task_dir: str | None = None) -> str:
    """Extract all text from a PDF using pdfplumber.

    Returns concatenated text across pages, separated by '\\n\\n--- page N ---\\n\\n'.
    Empty pages contribute nothing. Use re or string ops on the result to
    extract structured fields.
    """
    path = _resolve_path(filename, task_dir)
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber required for safe_read_pdf — install via pip")

    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"--- page {i + 1} ---\n{text}")
    return "\n\n".join(parts)


def safe_read_docx(filename: str, task_dir: str | None = None) -> str:
    """Extract paragraph text from a .docx file using python-docx.

    Returns paragraphs joined by newlines. Tables are appended after
    paragraphs as tab-separated rows for downstream parsing.
    """
    path = _resolve_path(filename, task_dir)
    try:
        from docx import Document
    except ImportError:
        raise ImportError("python-docx required for safe_read_docx — install via pip")

    doc = Document(path)
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append("\t".join(cells))
    return "\n".join(parts)


def safe_read_image(filename: str, task_dir: str | None = None) -> dict:
    """Probe an image file: returns size/mode/format metadata.

    Returns {'width', 'height', 'mode', 'format', 'path'}. For OCR / vision,
    use pytesseract or a vision model on the returned path. This helper is
    metadata-only and never raises on visual content.
    """
    path = _resolve_path(filename, task_dir)
    from PIL import Image
    with Image.open(path) as img:
        return {
            "path": path,
            "width": img.size[0],
            "height": img.size[1],
            "mode": img.mode,
            "format": img.format,
        }


# --- Quick exploration probes ---


def count_distinct(df: pd.DataFrame, col: str) -> dict:
    """Quick probe: rows vs distinct count for a column.

    Returns {'rows': int, 'distinct': int, 'ratio': float}.
    A ratio close to 1.0 indicates the column is a primary key /
    unique identifier in this DataFrame; lower ratios indicate
    repeated values (e.g. patient_id in a Laboratory table with
    multiple records per patient).
    """
    n = len(df)
    if n == 0:
        return {"rows": 0, "distinct": 0, "ratio": 0.0}
    nd = df[col].nunique(dropna=True)
    return {"rows": int(n), "distinct": int(nd), "ratio": round(nd / n, 3)}


def value_overlap(
    df_a: pd.DataFrame, col_a: str,
    df_b: pd.DataFrame, col_b: str,
) -> dict:
    """Quick probe: actual value overlap between two columns.

    Computes set-intersection on the FULL column data (not samples).
    Returns {'left_unique', 'right_unique', 'overlap', 'left_pct', 'right_pct'}.

    Use this to verify whether two columns are a real FK relationship
    (high left_pct or right_pct) or just shared by name (low both).
    Distinct from value_repr in manifest, which is sample-based.
    """
    set_a = set(df_a[col_a].dropna().unique())
    set_b = set(df_b[col_b].dropna().unique())
    inter = set_a & set_b
    left_pct = round(len(inter) / max(len(set_a), 1), 3)
    right_pct = round(len(inter) / max(len(set_b), 1), 3)
    return {
        "left_unique": len(set_a),
        "right_unique": len(set_b),
        "overlap": len(inter),
        "left_pct": left_pct,
        "right_pct": right_pct,
    }


def assume_then(claim: str, holds: bool) -> None:
    """Record an explicit assumption and whether it holds.

    Print a tagged line so the trace shows what the agent assumed and
    whether the data confirmed it. Helps surface wrong mental models
    early. Does not raise — execution continues either way.
    """
    tag = "ASSUME OK" if holds else "ASSUME FAIL"
    print(f"[{tag}] {claim}")


# --- Pickle helpers ---


def save_intermediate(data: Any, name: str, temp_dir: str | None = None) -> str:
    """Save intermediate result to TEMP_DIR as pickle.

    Args:
        data: Any picklable object.
        name: Short name (e.g., 'filtered_payments'). .pkl extension added automatically.
        temp_dir: Override for TEMP_DIR env var.

    Returns:
        Full path to saved file.
    """
    import pickle

    td = temp_dir or _ctx_temp_dir() or os.environ.get("TEMP_DIR", ".")
    if not name.endswith(".pkl"):
        name = f"{name}.pkl"
    path = os.path.join(td, name)
    with open(path, "wb") as f:
        pickle.dump(data, f)
    return path


def load_intermediate(name: str, temp_dir: str | None = None) -> Any:
    """Load intermediate result from TEMP_DIR.

    Args:
        name: Name used in save_intermediate (with or without .pkl).
        temp_dir: Override for TEMP_DIR env var.

    Returns:
        The unpickled object.
    """
    import pickle

    td = temp_dir or _ctx_temp_dir() or os.environ.get("TEMP_DIR", ".")
    if not name.endswith(".pkl"):
        name = f"{name}.pkl"
    path = os.path.join(td, name)
    with open(path, "rb") as f:
        return pickle.load(f)


# --- Structured result output ---


def save_result(
    answer: "dict | list | float | int | str",
    debug: "dict | None" = None,
    row_counts: "dict | None" = None,
    temp_dir: "str | None" = None,
) -> str:
    """Write structured step result to step_result.json in TEMP_DIR.

    Call this at the END of every step. It is the canonical output channel —
    Finalizer reads `answer`, Judge/sanity-checker reads `debug` and `row_counts`.
    stdout is still useful as a human-readable log, but is no longer parsed.

    Args:
        answer: The computed result.
                Table → {"col1": [v1, v2, ...], "col2": [v1, v2, ...]}
                Scalar → {"answer": [value]}
                Exploratory step with no final answer → {}
        debug:  Intermediate values for Judge verification.
                E.g., {"numerator": 5, "denominator": 8, "ratio": 0.625}
        row_counts: Filter statistics for sanity checks.
                E.g., {"rows_loaded": 1000, "after_filter": 6}

    Returns:
        Absolute path to the written file.
    """
    td = temp_dir or _ctx_temp_dir() or os.environ.get("TEMP_DIR", ".")
    path = os.path.join(td, "step_result.json")

    answer = _normalize_answer(answer)

    payload: dict = {
        "answer": _to_json_serializable(answer),
    }
    if debug:
        payload["debug"] = {str(k): _to_json_serializable(v) for k, v in debug.items()}
    if row_counts:
        payload["row_counts"] = {str(k): _to_json_serializable(v) for k, v in row_counts.items()}

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    answer_keys = list(payload["answer"].keys()) if isinstance(payload["answer"], dict) else type(payload["answer"]).__name__
    print(f"[step_result] saved — answer keys: {answer_keys}")
    return path


def _normalize_answer(answer: Any) -> Any:
    """Auto-expand common DataFrame-shaped inputs into dict-of-columns.

    The Finalizer expects {"col": [v1, v2, ...]} format. Agents often pass:
      - DataFrame directly             → convert with df.to_dict('list')
      - {"key": list_of_dicts}         → expand to {col1: [...], col2: [...]}
      - {"key": DataFrame}             → expand similarly
    Returns a dict-of-columns when expansion is unambiguous; otherwise unchanged.
    """
    # Bare DataFrame
    if isinstance(answer, pd.DataFrame):
        return {str(c): answer[c].tolist() for c in answer.columns}

    if not isinstance(answer, dict) or len(answer) != 1:
        return answer

    only_key, only_val = next(iter(answer.items()))

    # {"key": DataFrame}
    if isinstance(only_val, pd.DataFrame):
        return {str(c): only_val[c].tolist() for c in only_val.columns}

    # {"key": [{"col1":..,"col2":..}, ...]}
    if isinstance(only_val, list) and only_val and all(
        isinstance(item, dict) for item in only_val
    ):
        # Only expand when every dict shares the same keys (consistent rows)
        keys = list(only_val[0].keys())
        if all(set(item.keys()) == set(keys) for item in only_val):
            return {str(k): [item[k] for item in only_val] for k in keys}

    return answer


def _to_json_serializable(val: "Any") -> "Any":
    """Recursively convert numpy/pandas types to JSON-serializable Python types."""
    import math as _math
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return None if (isinstance(val, float) and _math.isnan(val)) else val
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        return [_to_json_serializable(v) for v in val]
    if isinstance(val, dict):
        return {str(k): _to_json_serializable(v) for k, v in val.items()}
    # pandas DataFrame → dict of lists
    if hasattr(val, "to_dict") and hasattr(val, "columns"):
        return {str(k): _to_json_serializable(v) for k, v in val.to_dict(orient="list").items()}
    # numpy array / pandas Series → list
    if hasattr(val, "tolist"):
        return _to_json_serializable(val.tolist())
    # numpy scalar
    if hasattr(val, "item"):
        return _to_json_serializable(val.item())
    return str(val)


# --- Private helpers ---


def _resolve_path(filename: str, task_dir: str | None = None) -> str:
    """Resolve filename to absolute path using TASK_DIR.

    Searches TASK_DIR root first, then all subdirectories.
    This handles tasks where data lives in context/, json/, etc.
    """
    if os.path.isabs(filename):
        return filename
    td = task_dir or _ctx_task_dir() or os.environ.get("TASK_DIR", ".")

    # Direct path first
    direct = os.path.join(td, filename)
    if os.path.exists(direct):
        return direct

    # Search subdirectories (common: context/, json/, data/)
    for root, _dirs, files in os.walk(td):
        if filename in files:
            return os.path.join(root, filename)

    # Fallback to direct path (will fail with clear FileNotFoundError)
    return direct
