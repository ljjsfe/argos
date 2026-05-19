"""SC1-B reference (Opus, NO helpers) for task_352 — pure pandas + regex."""
import re
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_352"
OUT = REPO / "eval_split/skeletons/_pred_task_352_B"
OUT.mkdir(parents=True, exist_ok=True)

event_df = pd.read_csv(TASK / "context/csv/event.csv")
md = (TASK / "context/doc/budget.md").read_text()

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
    "event record", "event link", "event tracking ID",
    "archived under the event", "documentation found under",
    "documentation is filed under", "reference code rec",
]

sentences = re.split(r"(?<=[.!?])\s+", md)
current_budget: str | None = None
amount_per_budget: dict[str, float] = {}
ad_set: set[str] = set()
link_event: dict[str, str] = {}

for sent in sentences:
    rids = re.findall(REC_RE, sent)
    if rids:
        if (len(rids) == 1 and any(kw in sent for kw in EVENT_LINK_KEYWORDS)
                and current_budget and rids[0] != current_budget):
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

yk = event_df[event_df["event_name"].str.contains("Yearly Kickoff", na=False)]["event_id"].iloc[0]
om = event_df[event_df["event_name"].str.contains("October Meeting", na=False)]["event_id"].iloc[0]

yk_total = sum(amount_per_budget.get(b, 0) for b in ad_set if link_event.get(b) == yk)
om_total = sum(amount_per_budget.get(b, 0) for b in ad_set if link_event.get(b) == om)
ratio = yk_total / om_total if om_total else 0.0

pd.DataFrame({"ratio": [ratio]}).to_csv(OUT / "prediction.csv", index=False)
print(f"ratio = {ratio}")
