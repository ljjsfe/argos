"""SC1-A reference (Opus + helpers) for task_418.

Q: Among the patients whose creatinine level is abnormal, how many of them
   aren't 70 yet?

Gold: 1.

Semantic decisions (the trick):
  - "abnormal creatinine" = HIGH abnormal (CRE > 1.2 mg/dL).
    Standard medical: 0.6-1.2 mg/dL. In this dataset, "abnormal" in
    SLE/thrombosis context means renal impairment = elevated CRE only.
    Low CRE (< 0.6) is benign.
  - "aren't 70 yet" = age < 70 at latest lab date (latest sample year in
    Laboratory.md = 1999).

Data is in two MARKDOWN narratives (no structured CSV/DB):
  - Laboratory.md: per-patient lab values described in prose, often with
    "initially logged as X but corrected to Y" pattern (last-wins).
  - Patient.md: per-patient demographics, with birth year in prose.

This task tests *narrative-to-structured* parsing, an information-extraction
gap currently with no helper coverage.
"""
import re
import sys
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_418_A"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LAB_MD = (REPO / "public/input/task_418/context/doc/Laboratory.md").read_text()
PAT_MD = (REPO / "public/input/task_418/context/doc/Patient.md").read_text()

PID_RE = re.compile(r"patient\s+(\d{3,8})|file\s+number\s+(\d{3,8})|Medical\s+Record\s+Number\s+(\d{3,8})", re.I)
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


def parse_first_pid(sentence: str) -> str | None:
    m = PID_RE.search(sentence)
    if not m:
        return None
    return next(g for g in m.groups() if g)


def parse_lab() -> tuple[dict[str, list[tuple[float, int]]], int]:
    sentences = re.split(r"(?<=[.!?])\s+", LAB_MD)
    cre_by_pid: dict[str, list[tuple[float, int]]] = {}
    current_pid: str | None = None
    current_year: int | None = None
    max_year = 0
    for s in sentences:
        pid = parse_first_pid(s)
        if pid:
            current_pid = pid
        ym = YEAR_RE.search(s)
        if ym:
            current_year = int(ym.group(1))
            max_year = max(max_year, current_year)
        if not current_pid:
            continue
        cre = None
        for pat in CRE_PATTERNS:
            for m in re.finditer(pat, s, re.I):
                cre = float(m.group(1))
        if cre is not None and current_year is not None:
            cre_by_pid.setdefault(current_pid, []).append((cre, current_year))
    return cre_by_pid, max_year


def parse_birth() -> dict[str, int]:
    sentences = re.split(r"(?<=[.!?])\s+", PAT_MD)
    birth: dict[str, int] = {}
    current_pid: str | None = None
    for s in sentences:
        pid = parse_first_pid(s)
        if pid:
            current_pid = pid
        if not current_pid:
            continue
        for pat in BIRTH_PATTERNS:
            for m in re.finditer(pat, s, re.I):
                birth[current_pid] = int(m.group(1))
    return birth


cre_by_pid, max_lab_year = parse_lab()
birth_year = parse_birth()

# Reference year for age = latest lab year (1999 in this data)
ref_year = max_lab_year

abnormal_pids = {pid for pid, recs in cre_by_pid.items() if any(c > 1.2 for c, _ in recs)}
result_pids = {
    pid for pid in abnormal_pids
    if pid in birth_year and 0 < (ref_year - birth_year[pid]) < 70
}

count = len(result_pids)
pd.DataFrame({"count": [count]}).to_csv(OUT_DIR / "prediction.csv", index=False)
print(f"abnormal-CRE + age<70: {count}  (gold = 1)  pids={result_pids}")
