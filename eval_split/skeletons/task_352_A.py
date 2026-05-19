"""SC1-A reference (Opus + helpers) for task_352.

Q: How many times was the budget in Advertisement for "Yearly Kickoff"
   meeting more than "October Meeting"?

Gold: 2.727272727272727 (= 150 / 55).

Trick: budget data is in narrative budget.md, NOT in cards.db (which is an
empty DuckDB file) or any structured table. Need to parse markdown prose to
extract: budget_id, category, link_to_event_id, amount.

Sentence-level parse:
  - Sentence introducing a budget asset → set current_budget (first rec_id).
  - Sentence with category word ('Advertisement') → mark category.
  - Amount sentences (incl. continuation without rec_id) → last-wins so the
    REVISED amount overrides PROVISIONAL.
  - Event-link sentence ('event record/link/tracking ID rec...') → bind
    current_budget to that event_id.
"""
import re
import sys
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_352_A"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_352"), str(OUT_DIR))

event_df = safe_read_csv("event.csv")
md = (REPO / "public/input/task_352/context/doc/budget.md").read_text()

REC_RE = r"rec[A-Za-z0-9]{14,17}"
AMOUNT_PATTERNS = [
    r"final budget[^0-9]{0,30}an amount of\s+(\d+(?:\.\d+)?)",
    r"revised[^0-9]{0,60}(\d+(?:\.\d+)?)",
    r"an amount of\s+(\d+(?:\.\d+)?)",
    r"budgeted amount of\s+(\d+(?:\.\d+)?)",
    r"budgeted at\s+(\d+(?:\.\d+)?)",
    r"was allocated\s+(\d+(?:\.\d+)?)",
]
EVENT_LINK_KEYWORDS = [
    "event record",
    "event link",
    "event tracking ID",
    "archived under the event",
    "documentation found under",
    "documentation is filed under",
    "reference code rec",
]

sentences = re.split(r"(?<=[.!?])\s+", md)
current_budget: str | None = None
amount_per_budget: dict[str, float] = {}
ad_set: set[str] = set()
link_event: dict[str, str] = {}

for sent in sentences:
    rids = re.findall(REC_RE, sent)
    if rids:
        if (
            len(rids) == 1
            and any(kw in sent for kw in EVENT_LINK_KEYWORDS)
            and current_budget
            and rids[0] != current_budget
        ):
            link_event[current_budget] = rids[0]
            continue
        current_budget = rids[0]
        if "Advertisement" in sent:
            ad_set.add(current_budget)
    if not current_budget:
        continue
    for pat in AMOUNT_PATTERNS:
        for m in re.finditer(pat, sent):
            amount_per_budget[current_budget] = float(m.group(1))

yk_event = event_df[event_df["event_name"].str.contains("Yearly Kickoff", na=False)]["event_id"].iloc[0]
om_event = event_df[event_df["event_name"].str.contains("October Meeting", na=False)]["event_id"].iloc[0]

yk_total = sum(amount_per_budget.get(b, 0) for b in ad_set if link_event.get(b) == yk_event)
om_total = sum(amount_per_budget.get(b, 0) for b in ad_set if link_event.get(b) == om_event)
ratio = yk_total / om_total if om_total else 0.0

pd.DataFrame({"ratio": [ratio]}).to_csv(OUT_DIR / "prediction.csv", index=False)
print(f"YK={yk_total}, OM={om_total}, ratio={ratio}  (gold = 2.727272727272727)")
