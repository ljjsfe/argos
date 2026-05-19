"""SC1-A reference (Opus + helpers) for task_344.

Q: Among the male patients who have a normal level of white blood cells,
   how many of them have an abnormal fibrinogen level?

Gold: 4.

CRITICAL DATA GAP (verified): only 3 distinct male patients have any FG
values in Laboratory.csv (IDs 4618443, 4865142, 5092228). Max possible
answer from provided files = 3, regardless of which range thresholds we
use. The gold value of 4 implies either (a) the gold uses an unspecified
additional data source (e.g. Examination.csv referenced in knowledge.md
but absent from input/), or (b) the gold is wrong.

This is a *data-completeness* failure, not a helper-expressiveness failure.
"""
import sys
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_344_A"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_344"), str(OUT_DIR))

psx = pd.read_csv(REPO / "public/input/task_344/context/patient_sex.csv")
lab = safe_read_csv("Laboratory.csv")

# Standard medical ranges:
#   WBC normal: 3.5–9.0 (×10^3/μL)
#   FG  normal: 200–400 mg/dL
# Per-patient: ANY normal WBC + ANY abnormal FG.
males = set(psx[psx["SEX"] == "M"]["ID"])
lab_m = lab[lab["ID"].isin(males)]

normal_wbc_ids = set(
    lab_m[(lab_m["WBC"] >= 3.5) & (lab_m["WBC"] <= 9.0)]["ID"].unique()
)
abn_fg_ids = set(
    lab_m[lab_m["FG"].notna() & ((lab_m["FG"] < 200) | (lab_m["FG"] > 400))]["ID"].unique()
)
count = len(normal_wbc_ids & abn_fg_ids)

pd.DataFrame({"count": [count]}).to_csv(OUT_DIR / "prediction.csv", index=False)
print(f"answer = {count}  (gold = 4)")
