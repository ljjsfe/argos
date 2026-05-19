# SC1-C2 LOGIC SKELETON for task_180 — Qwen fills the TODOs.
# Question: For all the people who paid more than 29.00 per unit of product id No.5,
#           give their consumption status in the August of 2012.

import sqlite3
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_180"
OUT = REPO / "eval_split/skeletons/_pred_task_180_C2"
OUT.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(TASK / "context/db/transactions_1k.db")
transactions = pd.read_sql("SELECT * FROM transactions_1k", conn)
conn.close()
yearmonth = pd.read_csv(TASK / "context/csv/yearmonth.csv")

# PROBE
print("transactions cols:", transactions.columns.tolist())
print(transactions.head(3))
print("yearmonth cols:", yearmonth.columns.tolist())
print(yearmonth.head(3))

# --------------------------------------------------------------
# DECOMPOSITION — fill the TODOs
# --------------------------------------------------------------

# Step 1: restrict `transactions` to ProductID=5. Then compute price-per-unit
#         (= Price / Amount). Be careful: if Amount==0 the per-unit is undefined,
#         skip those rows.
prod5_per_unit = None  # <TODO: a DataFrame with columns including CustomerID and a per-unit price>

# Step 2: identify the set of CustomerIDs whose per-unit price > 29.00.
hi_customers = None  # <TODO: a set or list of CustomerID values>

# Step 3: filter `yearmonth` to rows where Date == '201208' (August 2012) AND
#         CustomerID is in hi_customers.
result_rows = None  # <TODO: a DataFrame with the 'Consumption' column>

# Step 4: output ONLY the 'Consumption' column.
result_rows[["Consumption"]].to_csv(OUT / "prediction.csv", index=False)
