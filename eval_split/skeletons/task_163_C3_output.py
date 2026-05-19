# SC1-C2 LOGIC SKELETON for task_163 — Qwen fills the TODOs.
# Question: Identify the type of expenses and their total value approved for 'October Meeting' event.

import json, sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_163"
OUT = REPO / "eval_split/skeletons/_pred_task_163_C3"
OUT.mkdir(parents=True, exist_ok=True)

with open(TASK / "context/json/budget.json") as f:
    _d = json.load(f)
budget = pd.DataFrame(_d["records"] if isinstance(_d, dict) and "records" in _d else _d)
expense = pd.read_csv(TASK / "context/csv/expense.csv")
conn = sqlite3.connect(TASK / "context/db/event.db")
event = pd.read_sql("SELECT * FROM event", conn)
conn.close()

# PROBE
print("budget cols:", budget.columns.tolist())
print(budget.head(3))
print("expense cols:", expense.columns.tolist())
print(expense.head(3))
print("event cols:", event.columns.tolist())
print(event.head(3))

# --------------------------------------------------------------
# DECOMPOSITION — fill the TODOs
# --------------------------------------------------------------
# Join graph: event ←(by event_id == budget.link_to_event)— budget
#             budget ←(by budget_id == expense.link_to_budget)— expense

# Step 1: find the event_id(s) for events whose name is 'October Meeting'.
# Defensive: verify dtype of event_name is str before comparison
if event['event_name'].dtype == object:
    oct_events = event[event['event_name'] == 'October Meeting']
else:
    # Fallback: try converting to string
    oct_events = event[event['event_name'].astype(str) == 'October Meeting']

oct_event_ids = oct_events['event_id'].tolist() if len(oct_events) > 0 else []
print(f"Step 1 - Found {len(oct_event_ids)} October Meeting event(s)")

# Step 2: find budget_ids whose link_to_event is in oct_event_ids.
# Defensive: verify dtype of link_to_event matches event_id dtype
if len(oct_event_ids) > 0:
    if budget['link_to_event'].dtype == object:
        oct_budget_df = budget[budget['link_to_event'].isin(oct_event_ids)]
    else:
        oct_budget_df = budget[budget['link_to_event'].astype(str).isin([str(e) for e in oct_event_ids])]
    oct_budget_ids = oct_budget_df['budget_id'].tolist()
else:
    oct_budget_ids = []
print(f"Step 2 - Found {len(oct_budget_ids)} budget(s) for October Meeting")

# Step 3: filter `expense` to rows that are (a) approved (boolean True / 'True')
#         AND (b) whose link_to_budget is in oct_budget_ids.
# Defensive: check dtype of 'approved' column
if len(oct_budget_ids) > 0:
    # Handle both boolean and string representations of 'approved'
    if expense['approved'].dtype == bool:
        approved_mask = expense['approved'] == True
    elif expense['approved'].dtype == object:
        # Try multiple string representations
        approved_mask = expense['approved'].isin(['True', 'true', 'TRUE', True])
    else:
        approved_mask = expense['approved'].astype(str).isin(['True', 'true', 'TRUE', '1'])
    
    # Filter by link_to_budget
    if expense['link_to_budget'].dtype == object:
        budget_mask = expense['link_to_budget'].isin(oct_budget_ids)
    else:
        budget_mask = expense['link_to_budget'].astype(str).isin([str(b) for b in oct_budget_ids])
    
    oct_approved_exp = expense[approved_mask & budget_mask].copy()
else:
    oct_approved_exp = expense.iloc[:0].copy()

print(f"Step 3 - Found {len(oct_approved_exp)} approved expense(s)")

# Step 4: join oct_approved_exp back to budget (to recover link_to_event), then
#         join to event (to recover event.type). Result needs columns: cost, type.
if len(oct_approved_exp) > 0:
    # Merge expense with budget on link_to_budget -> budget_id
    joined = oct_approved_exp.merge(
        budget[['budget_id', 'link_to_event']], 
        left_on='link_to_budget', 
        right_on='budget_id', 
        how='left'
    )
    
    # Merge with event on link_to_event -> event_id
    joined = joined.merge(
        event[['event_id', 'type']], 
        left_on='link_to_event', 
        right_on='event_id', 
        how='left'
    )
    
    # Select only required columns
    joined = joined[['cost', 'type']].dropna(subset=['type'])
else:
    joined = pd.DataFrame(columns=['cost', 'type'])

print(f"Step 4 - Joined data shape: {joined.shape}")

# Step 5: group by type and sum cost.  Output column name for the sum is 'total_cost'.
if len(joined) > 0:
    result = joined.groupby('type', as_index=False)['cost'].sum()
    result.rename(columns={'cost': 'total_cost'}, inplace=True)
else:
    result = pd.DataFrame(columns=['type', 'total_cost'])

print(f"Step 5 - Final result:\n{result}")
print(f"Sanity check: Total rows={len(result)}, Total cost range=[{result['total_cost'].min()}, {result['total_cost'].max()}]")

result.to_csv(OUT / "prediction.csv", index=False)