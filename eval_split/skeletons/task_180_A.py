"""SC1-A reference (Opus + helpers) for task_180.

Q: For all the people who paid more than 29.00 per unit of product id No.5,
   give their consumption status in the August of 2012.

Per-unit = Price / Amount. Guard against Amount=0 (one row has Amount=0,
Price=11.20 → division-by-zero / data-quality issue. Gold excludes this customer,
implying Amount>0 is required.)

Aug 2012 = yearmonth.Date='201208'.
"""
import sys
import sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_180_A"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_180"), str(OUT_DIR))

conn = sqlite3.connect(REPO / "public/input/task_180/context/db/transactions_1k.db")
prod5 = pd.read_sql(
    "SELECT CustomerID, Price, Amount FROM transactions_1k "
    "WHERE ProductID=5 AND Amount>0",
    conn,
)
conn.close()
prod5["per_unit"] = prod5["Price"] / prod5["Amount"]
hi_customers = prod5[prod5["per_unit"] > 29.00]["CustomerID"].unique()

yearmonth = safe_read_csv("yearmonth.csv")
aug2012 = yearmonth[yearmonth["Date"].astype(str) == "201208"]
out = aug2012[aug2012["CustomerID"].isin(hi_customers)][["Consumption"]].copy()

out.to_csv(OUT_DIR / "prediction.csv", index=False)
print(f"{len(out)} rows  →  {sorted(out['Consumption'].tolist())}")
