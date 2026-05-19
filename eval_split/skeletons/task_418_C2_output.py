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
lab_data = {}
ref_year = 0

# Pattern to match patient IDs
pid_pattern = r'(?:patient|Medical Record Number)\s+([0-9]+)'

# Pattern to match creatinine values (various formats)
cre_patterns = [
    r'creatinine\s*[:\s=]*([0-9.]+)',
    r'CRE\s*[:\s=]*([0-9.]+)',
    r'creatinine\s+([0-9.]+)',
    r'([0-9.]+)\s*mg/dL.*creatinine',
]

# Pattern to match years (4 digits)
year_pattern = r'\b(19\d{2}|20\d{2})\b'

# Split by paragraphs/sections to track patient context
paragraphs = re.split(r'\n\s*\n', lab_md)

current_pid = None
for para in paragraphs:
    # Check for new patient ID in this paragraph
    pid_match = re.search(pid_pattern, para, re.IGNORECASE)
    if pid_match:
        current_pid = pid_match.group(1)
        if current_pid not in lab_data:
            lab_data[current_pid] = []
    
    # Look for creatinine values in this paragraph
    if current_pid:
        for cre_pat in cre_patterns:
            cre_matches = re.findall(cre_pat, para, re.IGNORECASE)
            for cre_val_str in cre_matches:
                try:
                    cre_value = float(cre_val_str)
                    # Find year in this paragraph
                    year_matches = re.findall(year_pattern, para)
                    if year_matches:
                        lab_year = int(year_matches[-1])
                        ref_year = max(ref_year, lab_year)
                        lab_data[current_pid].append((cre_value, lab_year))
                except ValueError:
                    pass

# If no year found, use a reasonable default
if ref_year == 0:
    ref_year = 2024

# Step 2: parse Patient.md into dict pid -> birth_year.
patient_birth = {}

# Pattern to match birth dates
birth_pattern = r'born\s+(?:on\s+)?(?:the\s+)?(\w+)\s+(\d+)(?:st|nd|rd|th)?,?\s*(\d{4})'

# Split by paragraphs to track patient context
patient_paragraphs = re.split(r'\n\s*\n', patient_md)

current_pid = None
for para in patient_paragraphs:
    # Check for new patient ID
    pid_match = re.search(pid_pattern, para, re.IGNORECASE)
    if pid_match:
        current_pid = pid_match.group(1)
    
    # Look for birth date in this paragraph
    if current_pid:
        birth_match = re.search(birth_pattern, para, re.IGNORECASE)
        if birth_match:
            birth_year = int(birth_match.group(3))
            patient_birth[current_pid] = birth_year

# Step 3: identify patient IDs that have at least one CRE > 1.2.
abnormal_pids = set()
for pid, measurements in lab_data.items():
    for cre_value, _ in measurements:
        if cre_value > 1.2:
            abnormal_pids.add(pid)
            break

# Step 4: among abnormal_pids, count those whose age = ref_year - birth_year < 70.
count = 0
for pid in abnormal_pids:
    if pid in patient_birth:
        age = ref_year - patient_birth[pid]
        if age < 70:
            count += 1

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)