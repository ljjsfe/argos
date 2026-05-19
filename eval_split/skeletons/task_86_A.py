"""SC1-A reference (Opus + helpers) for task_86.

Question: Which race was Alex Yoong in when he was in track number less than 20?

Semantic mapping:
  "track number" → driverStandings.position (finishing position in race)
  NOT round, NOT circuitId, NOT raceId.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))

import pandas as pd

from data_helpers import safe_read_csv, safe_read_json_df, set_task_context

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_86_A"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_86"), str(OUT_DIR))

drivers = safe_read_json_df("drivers.json")
standings = safe_read_csv("driverStandings.csv")
races = safe_read_csv("races.csv")

yoong = drivers[
    (drivers["forename"].str.lower() == "alex")
    & (drivers["surname"].str.lower() == "yoong")
]
yoong_id = int(yoong["driverId"].iloc[0])

yoong_low_pos = standings[
    (standings["driverId"] == yoong_id) & (standings["position"] < 20)
]
race_ids = yoong_low_pos["raceId"].unique().tolist()
race_names = races[races["raceId"].isin(race_ids)]["name"].tolist()

pd.DataFrame({"name": race_names}).to_csv(OUT_DIR / "prediction.csv", index=False)
print(f"Wrote {OUT_DIR / 'prediction.csv'} — {len(race_names)} rows")
