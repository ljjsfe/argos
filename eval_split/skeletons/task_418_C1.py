# SC1-C1 API SKELETON for task_418 — Qwen fills the TODOs.
# Question: Among the patients whose creatinine level is abnormal,
#           how many of them aren't 70 yet?

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
import pandas as pd

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_418_C1"
OUT_DIR.mkdir(parents=True, exist_ok=True)

lab_md = (REPO / "public/input/task_418/context/doc/Laboratory.md").read_text()
patient_md = (REPO / "public/input/task_418/context/doc/Patient.md").read_text()

# PROBE
print("Laboratory.md length:", len(lab_md), "chars; first 500 chars:")
print(lab_md[:500])
print("Patient.md length:", len(patient_md), "chars; first 500 chars:")
print(patient_md[:500])

# NOTE: both files are NARRATIVE prose. Lab.md describes per-patient lab results
# (e.g. "the creatinine, initially thought to be X, was verified at Y"). Patient.md
# describes per-patient demographics (birth year, sex).

# TODO: parse Laboratory.md to get a dict: patient_id -> [(creatinine_value, lab_year), ...]
lab_data = None  # <TODO>

# TODO: parse Patient.md to get a dict: patient_id -> birth_year
patient_birth = None  # <TODO>

# TODO: identify patients with ABNORMAL creatinine (use medical reference range).
abnormal_pids = None  # <TODO>

# TODO: among those, count distinct patients whose age < 70.
count = None  # <TODO>

# Write final answer (single integer)
pd.DataFrame({"count": [count]}).to_csv(OUT_DIR / "prediction.csv", index=False)
