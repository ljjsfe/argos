# SC1-C1 API SKELETON for task_352 — Qwen fills the TODOs.
# Question: How many times was the budget in Advertisement for "Yearly Kickoff" meeting
#           more than "October Meeting"?

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context
import pandas as pd
import re

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
parsed_budgets = {}

# Pattern to match budget entries with rec_id, category, amount, and event reference
# Looking for patterns like: recXXXXX, Category: XXX, Amount: $XXX, Event: recYYYYY
budget_entries = re.findall(
    r'(rec[A-Za-z0-9]+)[^a-zA-Z]*(?:Category|Item|Type)[^a-zA-Z]*([A-Za-z\s]+?)[^a-zA-Z]*(?:Amount|Value|Total)[^a-zA-Z]*\$?([\d,]+(?:\.\d{2})?)[^a-zA-Z]*(?:Event|linked|refers|associated)[^a-zA-Z]*(rec[A-Za-z0-9]+)',
    budget_md,
    re.IGNORECASE
)

for match in budget_entries:
    rec_id, category, amount_str, event_ref = match
    try:
        amount = float(amount_str.replace(',', ''))
        parsed_budgets[rec_id] = {
            'category': category.strip(),
            'amount': amount,
            'link_to_event': event_ref.strip()
        }
    except ValueError:
        continue

# TODO: find the event_id for 'Yearly Kickoff' and 'October Meeting' from `event`.
yk_event_id = None
om_event_id = None

for idx, row in event.iterrows():
    if 'Yearly Kickoff' in str(row['event_name']):
        yk_event_id = row['event_id']
    if 'October Meeting' in str(row['event_name']):
        om_event_id = row['event_id']

# TODO: compute total Advertisement amount for each event and the ratio (YK / OM).
yk_ad_total = 0.0
om_ad_total = 0.0

for rec_id, budget_info in parsed_budgets.items():
    if budget_info['category'].strip().lower() == 'advertisement':
        if budget_info['link_to_event'] == yk_event_id:
            yk_ad_total += budget_info['amount']
        elif budget_info['link_to_event'] == om_event_id:
            om_ad_total += budget_info['amount']

if om_ad_total > 0:
    ratio = yk_ad_total / om_ad_total
else:
    ratio = float('inf') if yk_ad_total > 0 else 0.0

# Write final answer (single ratio)
pd.DataFrame({"ratio": [ratio]}).to_csv(OUT_DIR / "prediction.csv", index=False)