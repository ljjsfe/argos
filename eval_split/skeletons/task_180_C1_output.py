# SC1-C1 API SKELETON for task_180 — Qwen fills the TODOs.
# Question: For all the people who paid more than 29.00 per unit of product id No.5,
#           give their consumption status in the August of 2012.

import sys, sqlite3
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context
import pandas as pd

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_180_C1"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_180"), str(OUT_DIR))

conn = sqlite3.connect(REPO / "public/input/task_180/context/db/transactions_1k.db")
transactions = pd.read_sql("SELECT * FROM transactions_1k", conn)
conn.close()
yearmonth = safe_read_csv("yearmonth.csv")

# PROBE
print("transactions cols:", transactions.columns.tolist())
print(transactions.head(3))
print("yearmonth cols:", yearmonth.columns.tolist())
print(yearmonth.head(3))

# TODO: from `transactions`, find the customer IDs of people who paid > 29.00 per unit
#       for product id 5. (Per-unit means price / amount.)
hi_customers = transactions[(transactions['ProductID'] == 5) & 
                            (transactions['Price'] / transactions['Amount'] > 29.00)]['CustomerID'].unique()

# TODO: from `yearmonth`, get the consumption rows for August 2012 for those customers.
august_2012_consumption = yearmonth[(yearmonth['Date'] == '201208') & 
                                     (yearmonth['CustomerID'].isin(hi_customers))]

# Write final answer
august_2012_consumption.to_csv(OUT_DIR / "prediction.csv", index=False)