# SC1-C2 LOGIC SKELETON for task_352 — Qwen fills the TODOs.
# Question: How many times was the budget in Advertisement for "Yearly Kickoff" meeting
#           more than "October Meeting"?

import re
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_352"
OUT = REPO / "eval_split/skeletons/_pred_task_352_C3"
OUT.mkdir(parents=True, exist_ok=True)

event = pd.read_csv(TASK / "context/csv/event.csv")
budget_md = (TASK / "context/doc/budget.md").read_text()

# PROBE
print("event cols:", event.columns.tolist())
print(event.head(3))
print("budget.md length:", len(budget_md), "chars; first 600 chars:")
print(budget_md[:600])

# --------------------------------------------------------------
# DECOMPOSITION — fill the TODOs
# --------------------------------------------------------------
# budget.md is NARRATIVE prose. Each budget record (rec_id) is introduced
# in one sentence; its category may appear in that same sentence (e.g.
# 'designated for Advertisement'); its amount may appear in a LATER sentence
# in the same paragraph, possibly with an initial value and a revised final
# value — the FINAL/REVISED value wins. The link to an event_id typically
# appears in yet another sentence (e.g. 'event record rec...').
#
# You must parse the prose into a structured map.

REC_RE = r"rec[A-Za-z0-9]{14,17}"  # rec_id pattern

# Step 1: split budget_md into sentences and walk them in order, tracking the
#         "current budget" rec_id. For each sentence:
#         - if it introduces a rec_id, update current_budget.
#         - if it contains 'Advertisement', mark current_budget as Advertisement.
#         - if it contains a "budgeted/allocated/amount of <N>" pattern, set
#           amount_per_budget[current_budget] = N (overriding any earlier value).
#         - if it contains 'event record <rec_id>' (or similar), set
#           link_event[current_budget] = that event rec_id.

parsed = {
    'amount_per_budget': {},
    'ad_set': {},
    'link_event': {}
}

current_budget = None

# Split into sentences (by . ! ? followed by space or end)
sentences = re.split(r'(?<=[.!?])\s+', budget_md)

for sentence in sentences:
    sentence = sentence.strip()
    if not sentence:
        continue
    
    # Check for rec_id introduction
    rec_match = re.search(REC_RE, sentence)
    if rec_match:
        current_budget = rec_match.group(0)
        parsed['amount_per_budget'][current_budget] = None
        parsed['ad_set'][current_budget] = False
        parsed['link_event'][current_budget] = None
    
    # Check for Advertisement category
    if current_budget and 'Advertisement' in sentence:
        parsed['ad_set'][current_budget] = True
    
    # Check for amount pattern (budgeted/allocated/amount of <number>)
    if current_budget:
        amount_match = re.search(r'(?:budgeted|allocated|amount)\s+of\s+(\d+(?:\.\d+)?)', sentence, re.IGNORECASE)
        if amount_match:
            parsed['amount_per_budget'][current_budget] = float(amount_match.group(1))
        
        # Check for event record link
        event_match = re.search(r'event\s+record\s+' + REC_RE, sentence, re.IGNORECASE)
        if event_match:
            parsed['link_event'][current_budget] = event_match.group(1)

# Sanity check: print some parsed data
print("Parsed budgets:", list(parsed['amount_per_budget'].keys())[:5])
print("Ad set keys:", [k for k, v in parsed['ad_set'].items() if v][:5])

# Step 2: look up the event_id for 'Yearly Kickoff' and for 'October Meeting'
#         from the `event` DataFrame.
yk_event_id = None
om_event_id = None

# Verify event_name column dtype
if 'event_name' in event.columns:
    # Convert to string for safe comparison
    event['event_name'] = event['event_name'].astype(str)
    
    # Search for Yearly Kickoff
    yk_mask = event['event_name'].str.contains('Yearly Kickoff', case=False, na=False)
    if yk_mask.any():
        yk_event_id = event.loc[yk_mask, 'event_id'].iloc[0]
    
    # Search for October Meeting
    om_mask = event['event_name'].str.contains('October Meeting', case=False, na=False)
    if om_mask.any():
        om_event_id = event.loc[om_mask, 'event_id'].iloc[0]

# Fallback: try partial matches if exact doesn't work
if yk_event_id is None:
    for name in event['event_name']:
        if isinstance(name, str) and 'Yearly' in name and 'Kickoff' in name:
            yk_event_id = name
            break

if om_event_id is None:
    for name in event['event_name']:
        if isinstance(name, str) and 'October' in name and 'Meeting' in name:
            om_event_id = name
            break

print(f"YK event_id: {yk_event_id}")
print(f"OM event_id: {om_event_id}")

# Step 3: sum the amounts of Advertisement-category budgets linked to each
#         event, then compute YK_total / OM_total.
yk_total = 0.0
om_total = 0.0

for rec_id, amount in parsed['amount_per_budget'].items():
    if amount is None:
        continue
    if parsed['ad_set'].get(rec_id, False):
        linked_event = parsed['link_event'].get(rec_id)
        if linked_event:
            if linked_event == yk_event_id:
                yk_total += amount
            elif linked_event == om_event_id:
                om_total += amount

# Handle division by zero
if om_total == 0:
    ratio = float('inf')
else:
    ratio = yk_total / om_total

# Sanity check before writing
print(f"Final Answer: ratio = {ratio}")
print(f"Sanity check: YK_total={yk_total}, OM_total={om_total}, rows_in_df={len(event)}")

pd.DataFrame({"ratio": [ratio]}).to_csv(OUT / "prediction.csv", index=False)