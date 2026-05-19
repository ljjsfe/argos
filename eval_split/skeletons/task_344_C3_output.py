# SC1-C2 LOGIC SKELETON for task_344 — Qwen fills the TODOs.
# Question: Among the male patients who have a normal level of white blood cells,
#           how many of them have an abnormal fibrinogen level?

from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_344"
OUT = REPO / "eval_split/skeletons/_pred_task_344_C3"
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
# Sanity check: confirm SEX column is string type before comparing
if patient_sex['SEX'].dtype == object:
    male_ids = set(patient_sex[patient_sex['SEX'] == 'M']['ID'].tolist())
else:
    # Fallback if SEX is not string
    male_ids = set(patient_sex[patient_sex['SEX'].astype(str) == 'M']['ID'].tolist())

# Step 2: among lab rows for male patients, identify patient IDs that have
#         at least one row with a NORMAL WBC value.
# First, filter lab rows for male patients
lab_male = laboratory[laboratory['ID'].isin(male_ids)]

# Sanity check: ensure we have rows after filtering
if len(lab_male) == 0:
    print("WARNING: No lab rows found for male patients, retrying with different approach")
    # Fallback: try direct merge
    lab_male = pd.merge(laboratory, patient_sex[['ID', 'SEX']], on='ID', how='inner')
    lab_male = lab_male[lab_male['SEX'] == 'M'].drop(columns=['SEX'])

# Ensure WBC is numeric
if lab_male['WBC'].dtype == object:
    lab_male['WBC'] = pd.to_numeric(lab_male['WBC'], errors='coerce')

# Find patients with at least one normal WBC value (3.5 - 9.0)
normal_wbc_mask = (lab_male['WBC'] >= 3.5) & (lab_male['WBC'] <= 9.0)
normal_wbc_male = set(lab_male.loc[normal_wbc_mask, 'ID'].unique().tolist())

# Sanity check: verify we found some normal WBC patients
if len(normal_wbc_male) == 0:
    print("WARNING: No patients with normal WBC found, checking WBC range")
    print("WBC min/max:", lab_male['WBC'].min(), lab_male['WBC'].max())

# Step 3: among lab rows for male patients, identify patient IDs that have
#         at least one row with an ABNORMAL FG (fibrinogen) value.
# Ensure FG is numeric
if lab_male['FG'].dtype == object:
    lab_male['FG'] = pd.to_numeric(lab_male['FG'], errors='coerce')

# Abnormal FG is outside 200-400 range (< 200 OR > 400)
abnormal_fg_mask = (lab_male['FG'] < 200) | (lab_male['FG'] > 400)
abnormal_fg_male = set(lab_male.loc[abnormal_fg_mask, 'ID'].unique().tolist())

# Sanity check: verify we found some abnormal FG patients
if len(abnormal_fg_male) == 0:
    print("WARNING: No patients with abnormal FG found, checking FG range")
    print("FG min/max:", lab_male['FG'].min(), lab_male['FG'].max())

# Step 4: count the intersection.
count = len(normal_wbc_male.intersection(abnormal_fg_male))

# Final sanity check before writing
print(f"Final Answer: {count} male patients with normal WBC have abnormal FG")
print(f"Sanity check: normal_wbc_male count={len(normal_wbc_male)}, abnormal_fg_male count={len(abnormal_fg_male)}")

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)