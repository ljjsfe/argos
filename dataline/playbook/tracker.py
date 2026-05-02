"""Playbook usage telemetry — fail-soft, file-backed.

Records (entry_id → {use_count, win_count}) so future review can prune
entries that are never retrieved or whose retrieval correlates with
failures. Data lives in a JSON file alongside the YAML.

This is operational metadata only. It does NOT affect retrieval scoring
(no preference for "popular" entries) — every entry stays equally
weighted until a human prunes it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


_DEFAULT_PATH = Path(__file__).parent / "playbook_telemetry.json"
_lock = threading.Lock()


def record_use(
    entry_ids: tuple[str, ...],
    won: bool,
    path: str | Path | None = None,
) -> None:
    """Increment use_count for each entry; if won, also win_count.

    Fail-soft: any I/O error is logged and swallowed — telemetry must
    never break the agent loop.
    """
    if not entry_ids:
        return

    p = Path(path) if path else _DEFAULT_PATH
    try:
        with _lock:
            data = _read(p)
            for eid in entry_ids:
                row = data.setdefault(eid, {"use_count": 0, "win_count": 0})
                row["use_count"] = int(row.get("use_count", 0)) + 1
                if won:
                    row["win_count"] = int(row.get("win_count", 0)) + 1
            _write(p, data)
    except OSError as e:
        logger.warning("playbook telemetry write failed: %s", e)


def read_telemetry(path: str | Path | None = None) -> dict[str, dict[str, int]]:
    """Read the current telemetry dict. Missing file → empty dict."""
    p = Path(path) if path else _DEFAULT_PATH
    return _read(p)


def _read(p: Path) -> dict[str, dict[str, int]]:
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            return {}
        return loaded
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("playbook telemetry read failed: %s", e)
        return {}


def _write(p: Path, data: dict[str, dict[str, int]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(str(tmp), str(p))
