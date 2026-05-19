# SC1-C1 API SKELETON for task_418 — Qwen fills the TODOs.
# Question: Among the patients whose creatinine level is abnormal,
#           how many of them aren't 70 yet?

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
import pandas as pd
import re

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
lab_data = {}
# Pattern to match patient ID, creatinine value, and year
lab_pattern = r'(?:Patient|Case|Subject|Record)\s*(?:Number|#|ID)?\s*(\d+).*?creatinine.*?(\d+\.?\d*)\s*(?:mg/dL|mg/dl|mmol/L).*?(\d{4})'
matches = re.findall(lab_pattern, lab_md, re.IGNORECASE | re.DOTALL)
for pid, value, year in matches:
    if pid not in lab_data:
        lab_data[pid] = []
    lab_data[pid].append((float(value), int(year)))

# TODO: parse Patient.md to get a dict: patient_id -> birth_year
patient_birth = {}
# Pattern to match Medical Record Number and birth year
patient_pattern = r'Medical Record Number\s*(\d+).*?(?:born|birth)[^\d]*(\d{4})'
matches = re.findall(patient_pattern, patient_md, re.IGNORECASE | re.DOTALL)
for mrn, birth_year in matches:
    patient_birth[mrn] = int(birth_year)

# TODO: identify patients with ABNORMAL creatinine (use medical reference range).
# Normal creatinine range: ~0.6-1.2 mg/dL for adults
abnormal_pids = set()
for pid, labs in lab_data.items():
    for value, year in labs:
        if value < 0.6 or value > 1.2:
            abnormal_pids.add(pid)
            break

# TODO: among those, count distinct patients whose age < 70.
count = 0
for pid in abnormal_pids:
    if pid in patient_birth:
        birth_year = patient_birth[pid]
        # Check if any lab reading shows age < 70
        for value, year in lab_data[pid]:
            if value < 0.6 or value > 1.2:
                age = year - birth_year
                if age < 70:
                    count += 1
                    break

# Write final answer (single integer)
pd.DataFrame({"count": [count]}).to_csv(OUT_DIR / "prediction.csv", index=False)