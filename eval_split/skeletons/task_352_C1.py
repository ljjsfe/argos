# SC1-C1 API SKELETON for task_352 — Qwen fills the TODOs.
# Question: How many times was the budget in Advertisement for "Yearly Kickoff" meeting
#           more than "October Meeting"?

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context
import pandas as pd

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_352_C1"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_352"), str(OUT_DIR))

event = safe_read_csv("event.csv")
budget_md = (REPO / "public/input/task_352/context/doc/budget.md").read_text()

# PROBE
print("event cols:", event.columns.tolist())
print(event.head(3))
print("budget.md length:", len(budget_md), "chars; first 500 chars:")
print(budget_md[:500])

# NOTE: budget records are described in NARRATIVE prose inside budget.md.
# Each budget has a rec_id, a category (e.g. 'Advertisement', 'Food'), an amount,
# and a linked event_id. Values may be revised mid-paragraph (final value wins).

# TODO: parse budget.md to build a dict: budget_id -> {'category': ..., 'amount': ..., 'link_to_event': ...}
parsed_budgets = None  # <TODO>

# TODO: find the event_id for 'Yearly Kickoff' and 'October Meeting' from `event`.
yk_event_id = None  # <TODO>
om_event_id = None  # <TODO>

# TODO: compute total Advertisement amount for each event and the ratio (YK / OM).
ratio = None  # <TODO>

# Write final answer (single ratio)
pd.DataFrame({"ratio": [ratio]}).to_csv(OUT_DIR / "prediction.csv", index=False)
