# SC1-C1 API SKELETON for task_163 — Qwen fills the TODOs.
# Question: Identify the type of expenses and their total value approved for 'October Meeting' event.

import sys, sqlite3
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, safe_read_json_df, set_task_context
import pandas as pd

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_163_C1"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_163"), str(OUT_DIR))

budget = safe_read_json_df("budget.json")
expense = safe_read_csv("expense.csv")
conn = sqlite3.connect(REPO / "public/input/task_163/context/db/event.db")
event = pd.read_sql("SELECT * FROM event", conn)
conn.close()

# PROBE
print("budget cols:", budget.columns.tolist())
print(budget.head(3))
print("expense cols:", expense.columns.tolist())
print(expense.head(3))
print("event cols:", event.columns.tolist())
print(event.head(3))

# TODO: filter expenses to those that are approved AND linked (via budget) to the 'October Meeting' event.
approved_oct_expenses = None  # <TODO>

# TODO: join with event so we can group by event type, then sum cost per type.
result = None  # <TODO>   # DataFrame with one row per (type, total_cost)

# Write final answer (2 columns: event type, total cost)
result.to_csv(OUT_DIR / "prediction.csv", index=False)
