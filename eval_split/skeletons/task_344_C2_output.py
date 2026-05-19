# SC1-C2 LOGIC SKELETON for task_344 — Qwen fills the TODOs.
# Question: Among the male patients who have a normal level of white blood cells,
#           how many of them have an abnormal fibrinogen level?

from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_344"
OUT = REPO / "eval_split/skeletons/_pred_task_344_C2"
OUT.mkdir(parents=True, exist_ok=True)

patient_sex = pd.read_csv(TASK / "context/patient_sex.csv")
laboratory = pd.read_csv(TASK / "context/csv/Laboratory.csv")

# PROBE
print("patient_sex cols:", patient_sex.columns.tolist())
print(patient_sex.head(3))
print("laboratory cols:", laboratory.columns.tolist())
print(laboratory.head(3))

# --------------------------------------------------------------
# DECOMPOSITION — fill the TODOs
# Standard medical reference ranges:
#   WBC normal:        3.5  – 9.0  (×10^3/μL)
#   FG (fibrinogen):   200  – 400  mg/dL
# --------------------------------------------------------------

# Step 1: from `patient_sex`, get the set of male patient IDs.
male_ids = set(patient_sex[patient_sex['SEX'] == 'M']['ID'].tolist())

# Step 2: among lab rows for male patients, identify patient IDs that have
#         at least one row with a NORMAL WBC value.
lab_for_males = laboratory[laboratory['ID'].isin(male_ids)]
normal_wbc_mask = (lab_for_males['WBC'] >= 3.5) & (lab_for_males['WBC'] <= 9.0)
normal_wbc_male = set(lab_for_males.loc[normal_wbc_mask, 'ID'].unique().tolist())

# Step 3: among lab rows for male patients, identify patient IDs that have
#         at least one row with an ABNORMAL FG (fibrinogen) value.
abnormal_fg_mask = ((lab_for_males['FG'] < 200) | (lab_for_males['FG'] > 400)) & lab_for_males['FG'].notna()
abnormal_fg_male = set(lab_for_males.loc[abnormal_fg_mask, 'ID'].unique().tolist())

# Step 4: count the intersection.
count = len(normal_wbc_male.intersection(abnormal_fg_male))

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)