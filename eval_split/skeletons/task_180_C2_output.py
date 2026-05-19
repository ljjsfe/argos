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
prod5 = transactions[transactions['ProductID'] == 5]
prod5_per_unit = prod5[prod5['Amount'] != 0].copy()
prod5_per_unit['per_unit_price'] = prod5_per_unit['Price'] / prod5_per_unit['Amount']

# Step 2: identify the set of CustomerIDs whose per-unit price > 29.00.
hi_customers = set(prod5_per_unit.loc[prod5_per_unit['per_unit_price'] > 29.00, 'CustomerID'])

# Step 3: filter `yearmonth` to rows where Date == '201208' (August 2012) AND
#         CustomerID is in hi_customers.
result_rows = yearmonth[(yearmonth['Date'] == '201208') & (yearmonth['CustomerID'].isin(hi_customers))]

# Step 4: output ONLY the 'Consumption' column.
result_rows[["Consumption"]].to_csv(OUT / "prediction.csv", index=False)