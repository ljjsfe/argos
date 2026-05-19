# SC1-C2 LOGIC SKELETON for task_352 — Qwen fills the TODOs.
# Question: How many times was the budget in Advertisement for "Yearly Kickoff" meeting
#           more than "October Meeting"?

import re
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_352"
OUT = REPO / "eval_split/skeletons/_pred_task_352_C2"
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
    'ad_set': set(),
    'link_event': {}
}

current_budget = None
sentences = re.split(r'(?<=[.!?])\s+', budget_md)

for sentence in sentences:
    # Check for rec_id introduction
    rec_matches = re.findall(REC_RE, sentence)
    if rec_matches:
        current_budget = rec_matches[-1]
        if current_budget not in parsed['link_event']:
            parsed['link_event'][current_budget] = None
    
    # Check for Advertisement category
    if 'Advertisement' in sentence:
        if current_budget:
            parsed['ad_set'].add(current_budget)
    
    # Check for amount pattern
    amount_match = re.search(r'(?:budgeted|allocated|amount of)\s+[$£€]?\s*([\d,]+(?:\.\d+)?)', sentence, re.IGNORECASE)
    if amount_match and current_budget:
        amount_str = amount_match.group(1).replace(',', '')
        parsed['amount_per_budget'][current_budget] = float(amount_str)
    
    # Check for event record link
    event_match = re.search(r'event\s+record\s+' + REC_RE, sentence, re.IGNORECASE)
    if event_match and current_budget:
        event_rec_id = event_match.group(1)
        parsed['link_event'][current_budget] = event_rec_id

# Step 2: look up the event_id for 'Yearly Kickoff' and for 'October Meeting'
#         from the `event` DataFrame.
yk_event_id = event[event['event_name'] == 'Yearly Kickoff']['event_id'].iloc[0]
om_event_id = event[event['event_name'] == 'October Meeting']['event_id'].iloc[0]

# Step 3: sum the amounts of Advertisement-category budgets linked to each
#         event, then compute YK_total / OM_total.
yk_total = 0.0
om_total = 0.0

for rec_id, amount in parsed['amount_per_budget'].items():
    if rec_id in parsed['ad_set']:
        linked_event = parsed['link_event'].get(rec_id)
        if linked_event:
            if linked_event == yk_event_id:
                yk_total += amount
            elif linked_event == om_event_id:
                om_total += amount

ratio = yk_total / om_total if om_total > 0 else 0.0

pd.DataFrame({"ratio": [ratio]}).to_csv(OUT / "prediction.csv", index=False)