"""SC1-A reference (Opus + helpers) for task_163.

Q: Identify the type of expenses and their total value approved for 'October Meeting' event.

Join graph: event(event_id, name, type) ←—link_to_event—— budget(budget_id) ←—link_to_budget—— expense(approved, cost).
"""
import sys
import sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, safe_read_json_df, set_task_context

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_163_A"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_163"), str(OUT_DIR))

budget = safe_read_json_df("budget.json")
expense = safe_read_csv("expense.csv")
conn = sqlite3.connect(REPO / "public/input/task_163/context/db/event.db")
event = pd.read_sql("SELECT event_id, event_name, type FROM event", conn)
conn.close()

oct_events = event[event["event_name"] == "October Meeting"]
oct_budgets = budget[budget["link_to_event"].isin(oct_events["event_id"])]

approved = expense[
    expense["approved"].astype(str).str.lower().isin(["true", "t", "1"])
]
oct_exp = approved[approved["link_to_budget"].isin(oct_budgets["budget_id"])]

joined = (
    oct_exp
    .merge(oct_budgets[["budget_id", "link_to_event"]],
           left_on="link_to_budget", right_on="budget_id", how="left")
    .merge(oct_events[["event_id", "type"]],
           left_on="link_to_event", right_on="event_id", how="left")
)
grouped = joined.groupby("type", as_index=False)["cost"].sum()
grouped = grouped.rename(columns={"cost": "total_cost"})

grouped.to_csv(OUT_DIR / "prediction.csv", index=False)
print(grouped.to_string())
