# SC1-C2 LOGIC SKELETON for task_418 — Qwen fills the TODOs.
# Question: Among the patients whose creatinine level is abnormal,
#           how many of them aren't 70 yet?

import re
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_418"
OUT = REPO / "eval_split/skeletons/_pred_task_418_C2"
OUT.mkdir(parents=True, exist_ok=True)

lab_md = (TASK / "context/doc/Laboratory.md").read_text()
patient_md = (TASK / "context/doc/Patient.md").read_text()

# PROBE
print("Laboratory.md length:", len(lab_md), "chars; first 600 chars:")
print(lab_md[:600])
print("Patient.md length:", len(patient_md), "chars; first 600 chars:")
print(patient_md[:600])

# --------------------------------------------------------------
# DECOMPOSITION — fill the TODOs
# Standard medical reference range:
#   Creatinine normal:   ~0.6 – 1.2 mg/dL.
#   ABNORMAL = high CRE (> 1.2) — low CRE is benign for this question.
# --------------------------------------------------------------
# Both Laboratory.md and Patient.md are NARRATIVE prose. A patient's identity
# is given as "patient <ID>" or "Medical Record Number <ID>". Within the same
# paragraph, the patient ID anchors all subsequent values until the next
# patient is mentioned. Values are often given as "initially X, corrected to Y"
# — the FINAL/CORRECTED value wins.
#
# Reference year for age = the MAX lab year mentioned in Laboratory.md.

# Step 1: parse Laboratory.md into dict pid -> list of (cre_value, lab_year)
#         tuples. Walk sentences in order; track the current patient anchor.
#         Also track the max lab year observed.
lab_data = None    # <TODO: dict[str, list[tuple[float, int]]]>
ref_year = None    # <TODO: int = max lab year>

# Step 2: parse Patient.md into dict pid -> birth_year.
patient_birth = None  # <TODO: dict[str, int]>

# Step 3: identify patient IDs that have at least one CRE > 1.2.
abnormal_pids = None  # <TODO: set of patient IDs>

# Step 4: among abnormal_pids, count those whose age = ref_year - birth_year < 70.
count = None  # <TODO: integer>

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)
