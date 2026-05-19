# SC1-C2 LOGIC SKELETON for task_86 — Qwen fills the TODOs.
# Question: Which race was Alex Yoong in when he was in track number less than 20?

import json
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_86"
OUT = REPO / "eval_split/skeletons/_pred_task_86_C2"
OUT.mkdir(parents=True, exist_ok=True)

with open(TASK / "context/json/drivers.json") as f:
    _d = json.load(f)
drivers = pd.DataFrame(_d["records"] if isinstance(_d, dict) and "records" in _d else _d)
standings = pd.read_csv(TASK / "context/csv/driverStandings.csv")
races = pd.read_csv(TASK / "context/csv/races.csv")

# PROBE
print("drivers cols:", drivers.columns.tolist())
print(drivers.head(3))
print("standings cols:", standings.columns.tolist())
print(standings.head(3))
print("races cols:", races.columns.tolist())
print(races.head(3))

# --------------------------------------------------------------
# DECOMPOSITION (do NOT modify the steps — only fill the TODOs)
# --------------------------------------------------------------

# Step 1: in `drivers`, locate the row for the person "Alex Yoong" and extract their driverId.
yoong_id = None  # <TODO: a single integer driverId>

# Step 2: in `standings`, filter to rows where (a) driverId equals Yoong's, AND
#         (b) the driver's track-number-related value is < 20.
#         NOTE: there is no literal column named "track number" — you must map
#         the question's "track number" concept to an actual standings column.
yoong_rows = None  # <TODO: a DataFrame, subset of standings>

# Step 3: get the *distinct* race names (column 'name' in races) corresponding to
#         the raceId values in yoong_rows.
race_names = None  # <TODO: a list[str], distinct race names>

# Step 4: write a single-column DataFrame named 'name' to prediction.csv.
pd.DataFrame({"name": race_names}).to_csv(OUT / "prediction.csv", index=False)
