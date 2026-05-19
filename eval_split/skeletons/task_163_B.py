"""SC1-B reference (Opus, NO helpers) for task_163 — pure pandas + sqlite3."""
import sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_163"
OUT = REPO / "eval_split/skeletons/_pred_task_163_B"
OUT.mkdir(parents=True, exist_ok=True)

import json
with open(TASK / "context/json/budget.json") as f:
    _d = json.load(f)
budget = pd.DataFrame(_d["records"] if isinstance(_d, dict) and "records" in _d else _d)
expense = pd.read_csv(TASK / "context/csv/expense.csv")
conn = sqlite3.connect(TASK / "context/db/event.db")
event = pd.read_sql("SELECT event_id, event_name, type FROM event", conn)
conn.close()

oct_events = event[event["event_name"] == "October Meeting"]
oct_budgets = budget[budget["link_to_event"].isin(oct_events["event_id"])]
approved = expense[expense["approved"].astype(str).str.lower().isin(["true", "t", "1"])]
oct_exp = approved[approved["link_to_budget"].isin(oct_budgets["budget_id"])]

joined = (
    oct_exp.merge(
        oct_budgets[["budget_id", "link_to_event"]],
        left_on="link_to_budget", right_on="budget_id", how="left",
    ).merge(
        oct_events[["event_id", "type"]],
        left_on="link_to_event", right_on="event_id", how="left",
    )
)
grouped = joined.groupby("type", as_index=False)["cost"].sum()
grouped = grouped.rename(columns={"cost": "total_cost"})
grouped.to_csv(OUT / "prediction.csv", index=False)
print(grouped.to_string())
