# SC1-C1 API SKELETON for task_86 — Qwen fills the TODOs.
# Question: Which race was Alex Yoong in when he was in track number less than 20?

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, safe_read_json_df, set_task_context
import pandas as pd

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_86_C1"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_86"), str(OUT_DIR))

drivers = safe_read_json_df("drivers.json")
standings = safe_read_csv("driverStandings.csv")
races = safe_read_csv("races.csv")

# PROBE
print("drivers cols:", drivers.columns.tolist())
print(drivers.head(3))
print("standings cols:", standings.columns.tolist())
print(standings.head(3))
print("races cols:", races.columns.tolist())
print(races.head(3))

# TODO: find Alex Yoong's driver id.
yoong_id = drivers[(drivers['forename'] == 'Alex') & (drivers['surname'] == 'Yoong')]['driverId'].iloc[0]

# TODO: identify the rows in `standings` for Alex Yoong with "track number" < 20.
yoong_low = standings.merge(drivers[['driverId', 'number']], on='driverId').query('number < 20')

# TODO: get the race names for those rows.
race_names = yoong_low.merge(races[['raceId', 'name']], on='raceId')['name'].tolist()

# Write final answer
pd.DataFrame({"name": race_names}).to_csv(OUT_DIR / "prediction.csv", index=False)