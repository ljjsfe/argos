# SC1-C2 LOGIC SKELETON for task_86 — Qwen fills the TODOs.
# Question: Which race was Alex Yoong in when he was in track number less than 20?

import json
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_86"
OUT = REPO / "eval_split/skeletons/_pred_task_86_C3"
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
# Defensive: Verify dtype of forename/surname columns are str before comparing
assert drivers['forename'].dtype == object or drivers['forename'].dtype == 'str', \
    f"forename dtype mismatch: {drivers['forename'].dtype}"
assert drivers['surname'].dtype == object or drivers['surname'].dtype == 'str', \
    f"surname dtype mismatch: {drivers['surname'].dtype}"
yoong_row = drivers[(drivers['forename'] == 'Alex') & (drivers['surname'] == 'Yoong')]
if len(yoong_row) == 0:
    # Fallback: try different case variations
    yoong_row = drivers[(drivers['forename'].str.lower() == 'alex') & (drivers['surname'].str.lower() == 'yoong')]
yoong_id = int(yoong_row['driverId'].iloc[0])

# Step 2: in `standings`, filter to rows where (a) driverId equals Yoong's, AND
#         (b) the driver's track-number-related value is < 20.
#         NOTE: there is no literal column named "track number" — you must map
#         the question's "track number" concept to an actual standings column.
# The 'number' column in drivers represents the car/track number
yoong_number = int(yoong_row['number'].iloc[0])
yoong_rows = standings[standings['driverId'] == yoong_id].copy()
# Sanity check: if filter produces 0 rows but we expect some, check dtype
if len(yoong_rows) == 0:
    print(f"Warning: No standings found for driverId={yoong_id}, checking dtype...")
    print(f"standings driverId dtype: {standings['driverId'].dtype}")
    print(f"yoong_id type: {type(yoong_id)}")
    # Try converting to match dtype
    if standings['driverId'].dtype != int:
        yoong_rows = standings[standings['driverId'].astype(int) == yoong_id]

# Only include rows if the driver's number is < 20
if yoong_number >= 20:
    yoong_rows = pd.DataFrame(columns=standings.columns)
    print(f"Driver number ({yoong_number}) is not < 20, returning empty result")

# Step 3: get the *distinct* race names (column 'name' in races) corresponding to
#         the raceId values in yoong_rows.
if len(yoong_rows) > 0:
    race_ids = yoong_rows['raceId'].unique()
    race_names = races[races['raceId'].isin(race_ids)]['name'].unique().tolist()
else:
    race_names = []

# Sanity check: print final answer
print(f"Final Answer: {race_names}")
print(f"Sanity check: {len(race_names)} distinct race(s) found")

# Step 4: write a single-column DataFrame named 'name' to prediction.csv.
pd.DataFrame({"name": race_names}).to_csv(OUT / "prediction.csv", index=False)