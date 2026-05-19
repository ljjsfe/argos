# SC1-C2 LOGIC SKELETON for task_163 — Qwen fills the TODOs.
# Question: Identify the type of expenses and their total value approved for 'October Meeting' event.

import json, sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_163"
OUT = REPO / "eval_split/skeletons/_pred_task_163_C2"
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
oct_event_ids = None  # <TODO: a list/Index of event_id values>

# Step 2: find budget_ids whose link_to_event is in oct_event_ids.
oct_budget_ids = None  # <TODO: a list/Index of budget_id values>

# Step 3: filter `expense` to rows that are (a) approved (boolean True / 'True')
#         AND (b) whose link_to_budget is in oct_budget_ids.
oct_approved_exp = None  # <TODO: a DataFrame, subset of expense>

# Step 4: join oct_approved_exp back to budget (to recover link_to_event), then
#         join to event (to recover event.type). Result needs columns: cost, type.
joined = None  # <TODO: a DataFrame with columns including 'cost' and 'type'>

# Step 5: group by type and sum cost.  Output column name for the sum is 'total_cost'.
result = None  # <TODO: a DataFrame with columns ['type', 'total_cost']>

result.to_csv(OUT / "prediction.csv", index=False)
