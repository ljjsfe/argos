"""SC1-B reference (Opus, NO helpers) for task_418 — pure regex + pandas.

task_418_A.py used no helpers (pure markdown regex parse), so B == A.
"""
import re
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_418"
OUT = REPO / "eval_split/skeletons/_pred_task_418_B"
OUT.mkdir(parents=True, exist_ok=True)

LAB_MD = (TASK / "context/doc/Laboratory.md").read_text()
PAT_MD = (TASK / "context/doc/Patient.md").read_text()

PID_RE = re.compile(
    r"patient\s+(\d{3,8})|file\s+number\s+(\d{3,8})|Medical\s+Record\s+Number\s+(\d{3,8})",
    re.I,
)
YEAR_RE = re.compile(r"\b(19[89][0-9]|20[01][0-9])\b")
BIRTH_PATTERNS = [
    r"born[^.]*?\b(19\d{2}|20\d{2})\b",
    r"date of birth[^.]*?\b(19\d{2}|20\d{2})\b",
    r"birthday[^.]*?\b(19\d{2}|20\d{2})\b",
    r"birth(?:date)?[^.]*?\b(19\d{2}|20\d{2})\b",
]
CRE_PATTERNS = [
    r"creatinine[^.]*?verified at\s+(\d+(?:\.\d+)?)",
    r"creatinine[^.]*?corrected to\s+(\d+(?:\.\d+)?)",
    r"creatinine[^.]*?adjusted to\s+(\d+(?:\.\d+)?)",
    r"CRE[^.]*?to a final value of\s+(\d+(?:\.\d+)?)",
    r"creatinine[^.]*?was\s+(\d+(?:\.\d+)?)\s+mg",
    r"creatinine[^.]*?at\s+(\d+(?:\.\d+)?)\s+mg",
    r"CRE\s+(?:reading\s+)?(?:was|of)\s+(\d+(?:\.\d+)?)",
]


def _first_pid(sent: str) -> str | None:
    m = PID_RE.search(sent)
    if not m:
        return None
    return next(g for g in m.groups() if g)


def parse_lab():
    sents = re.split(r"(?<=[.!?])\s+", LAB_MD)
    by_pid: dict[str, list[tuple[float, int]]] = {}
    pid, year, mx = None, None, 0
    for s in sents:
        p = _first_pid(s)
        if p:
            pid = p
        ym = YEAR_RE.search(s)
        if ym:
            year = int(ym.group(1))
            mx = max(mx, year)
        if not pid:
            continue
        cre = None
        for pat in CRE_PATTERNS:
            for m in re.finditer(pat, s, re.I):
                cre = float(m.group(1))
        if cre is not None and year is not None:
            by_pid.setdefault(pid, []).append((cre, year))
    return by_pid, mx


def parse_birth():
    sents = re.split(r"(?<=[.!?])\s+", PAT_MD)
    birth: dict[str, int] = {}
    pid = None
    for s in sents:
        p = _first_pid(s)
        if p:
            pid = p
        if not pid:
            continue
        for pat in BIRTH_PATTERNS:
            for m in re.finditer(pat, s, re.I):
                birth[pid] = int(m.group(1))
    return birth


cre_by_pid, max_lab_year = parse_lab()
birth_year = parse_birth()
ref_year = max_lab_year

abnormal_pids = {pid for pid, recs in cre_by_pid.items() if any(c > 1.2 for c, _ in recs)}
result_pids = {
    pid for pid in abnormal_pids
    if pid in birth_year and 0 < (ref_year - birth_year[pid]) < 70
}
count = len(result_pids)

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)
print(f"count = {count}  (gold = 1)")
