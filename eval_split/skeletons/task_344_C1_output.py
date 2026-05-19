# SC1-C1 API SKELETON for task_344 — Qwen fills the TODOs.
# Question: Among the male patients who have a normal level of white blood cells,
#           how many of them have an abnormal fibrinogen level?

import sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "dataline" / "helpers"))
from data_helpers import safe_read_csv, set_task_context
import pandas as pd

OUT_DIR = REPO / "eval_split/skeletons/_pred_task_344_C1"
OUT_DIR.mkdir(parents=True, exist_ok=True)
set_task_context(str(REPO / "public/input/task_344"), str(OUT_DIR))

patient_sex = pd.read_csv(REPO / "public/input/task_344/context/patient_sex.csv")
laboratory = safe_read_csv("Laboratory.csv")

# PROBE
print("patient_sex cols:", patient_sex.columns.tolist())
print(patient_sex.head(3))
print("laboratory cols:", laboratory.columns.tolist())
print(laboratory.head(3))

# TODO: identify male patients.
male_ids = patient_sex[patient_sex['SEX'] == 'M']['ID'].unique()

# TODO: among male lab rows, find which patients have at least one row with a NORMAL WBC value.
#       (Use standard medical reference range for normal WBC.)
# Normal WBC: 4.0 - 11.0 (×10^9/L or cells/μL)
normal_wbc_male_ids = laboratory[
    (laboratory['ID'].isin(male_ids)) & 
    (laboratory['WBC'] >= 4.0) & 
    (laboratory['WBC'] <= 11.0)
]['ID'].unique()

# TODO: among those, count distinct patients who have at least one row with an ABNORMAL FG (fibrinogen) value.
#       (Use standard medical reference range for normal fibrinogen.)
# Normal FG: 200 - 400 mg/dL
abnormal_fg_patients = laboratory[
    (laboratory['ID'].isin(normal_wbc_male_ids)) & 
    ((laboratory['FG'] < 200) | (laboratory['FG'] > 400))
]['ID'].unique()
count = len(abnormal_fg_patients)   # integer

# Write final answer (single integer)
pd.DataFrame({"count": [count]}).to_csv(OUT_DIR / "prediction.csv", index=False)