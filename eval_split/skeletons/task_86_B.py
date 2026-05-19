"""SC1-B reference (Opus, NO helpers) for task_86 — pure pandas."""
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_86"
OUT = REPO / "eval_split/skeletons/_pred_task_86_B"
OUT.mkdir(parents=True, exist_ok=True)

import json
with open(TASK / "context/json/drivers.json") as f:
    _d = json.load(f)
drivers = pd.DataFrame(_d["records"] if isinstance(_d, dict) and "records" in _d else _d)
standings = pd.read_csv(TASK / "context/csv/driverStandings.csv")
races = pd.read_csv(TASK / "context/csv/races.csv")

yoong = drivers[
    (drivers["forename"].str.lower() == "alex")
    & (drivers["surname"].str.lower() == "yoong")
]
yoong_id = int(yoong["driverId"].iloc[0])

low_pos = standings[(standings["driverId"] == yoong_id) & (standings["position"] < 20)]
race_names = races[races["raceId"].isin(low_pos["raceId"].unique())]["name"].tolist()

pd.DataFrame({"name": race_names}).to_csv(OUT / "prediction.csv", index=False)
print(f"{len(race_names)} rows")
