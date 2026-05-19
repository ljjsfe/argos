"""SC1-B reference (Opus, NO helpers) for task_180 — pure pandas + sqlite3."""
import sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_180"
OUT = REPO / "eval_split/skeletons/_pred_task_180_B"
OUT.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(TASK / "context/db/transactions_1k.db")
prod5 = pd.read_sql(
    "SELECT CustomerID, Price, Amount FROM transactions_1k "
    "WHERE ProductID=5 AND Amount>0",
    conn,
)
conn.close()
prod5["per_unit"] = prod5["Price"] / prod5["Amount"]
hi_customers = prod5[prod5["per_unit"] > 29.00]["CustomerID"].unique()

ym = pd.read_csv(TASK / "context/csv/yearmonth.csv")
aug = ym[ym["Date"].astype(str) == "201208"]
out = aug[aug["CustomerID"].isin(hi_customers)][["Consumption"]].copy()
out.to_csv(OUT / "prediction.csv", index=False)
print(f"{len(out)} rows")
