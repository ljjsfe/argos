"""SC1-B reference (Opus, NO helpers) for task_344 — pure pandas.

Returns 3 (data-gap, see task_344_A NOTES) — same as A.
"""
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_344"
OUT = REPO / "eval_split/skeletons/_pred_task_344_B"
OUT.mkdir(parents=True, exist_ok=True)

psx = pd.read_csv(TASK / "context/patient_sex.csv")
lab = pd.read_csv(TASK / "context/csv/Laboratory.csv")

males = set(psx[psx["SEX"] == "M"]["ID"])
lab_m = lab[lab["ID"].isin(males)]

normal_wbc_ids = set(lab_m[(lab_m["WBC"] >= 3.5) & (lab_m["WBC"] <= 9.0)]["ID"].unique())
abn_fg_ids = set(
    lab_m[lab_m["FG"].notna() & ((lab_m["FG"] < 200) | (lab_m["FG"] > 400))]["ID"].unique()
)
count = len(normal_wbc_ids & abn_fg_ids)

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)
print(f"answer = {count}  (gold = 4)")
