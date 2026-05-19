# SC1-C2 LOGIC SKELETON for task_418 — Qwen fills the TODOs.
# Question: Among the patients whose creatinine level is abnormal,
#           how many of them aren't 70 yet?

import re
from pathlib import Path
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "public/input/task_418"
OUT = REPO / "eval_split/skeletons/_pred_task_418_C3"
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
lab_data = {}    # dict[str, list[tuple[float, int]]]
ref_year = None    # int = max lab year

# Parse Laboratory.md - look for patient IDs and creatinine values
current_pid = None
for line in lab_md.split('\n'):
    # Check for patient ID pattern - try multiple formats
    pid_match = re.search(r'(?:patient|Medical Record Number)\s+(\d+)', line, re.IGNORECASE)
    if pid_match:
        current_pid = pid_match.group(1)
        if current_pid not in lab_data:
            lab_data[current_pid] = []
    
    # Look for creatinine values with years when we have a current patient
    if current_pid:
        # Pattern for creatinine: "creatinine X.XX" or "CRE X.XX" 
        cre_pattern = r'(?:creatinine|CRE)\s*[:\-]?\s*([\d.]+)'
        cre_matches = re.findall(cre_pattern, line, re.IGNORECASE)
        
        # Pattern for year: look for 4-digit years
        year_pattern = r'\b(\d{4})\b'
        year_matches = re.findall(year_pattern, line)
        
        if cre_matches and year_matches:
            cre_value = float(cre_matches[-1])  # Use last match if multiple
            lab_year = int(year_matches[-1])    # Use last match if multiple
            
            lab_data[current_pid].append((cre_value, lab_year))
            
            # Update reference year
            if ref_year is None or lab_year > ref_year:
                ref_year = lab_year

# Fallback: if no labs found, check entire document for patterns
if ref_year is None:
    all_years = re.findall(r'\b(\d{4})\b', lab_md)
    if all_years:
        ref_year = max(int(y) for y in all_years)
    else:
        ref_year = 2024  # Default fallback

# Sanity check: verify we parsed some data
print(f"Parsed {len(lab_data)} patients from Laboratory.md")
print(f"Reference year: {ref_year}")

# Step 2: parse Patient.md into dict pid -> birth_year.
patient_birth = {}  # dict[str, int]

# Parse Patient.md - look for patient IDs and birth years
current_pid = None
for line in patient_md.split('\n'):
    # Check for patient ID pattern
    pid_match = re.search(r'(?:patient|Medical Record Number)\s+(\d+)', line, re.IGNORECASE)
    if pid_match:
        current_pid = pid_match.group(1)
    
    # Look for birth date/year when we have a current patient
    if current_pid:
        # Pattern for birth: "born on [date]" or "birth date:"
        # Try to extract year from various date formats
        birth_patterns = [
            r'born\s+(?:on\s+)?(?:July|August|September|October|November|December|January|February|March|April|May|June)\s+\d{1,2},?\s*(\d{4})',
            r'born\s+(\d{4})',
            r'birth\s+date[:\s]+(\d{4})',
            r'(\d{4})\s*birth',
        ]
        
        for pattern in birth_patterns:
            birth_match = re.search(pattern, line, re.IGNORECASE)
            if birth_match:
                birth_year = int(birth_match.group(1))
                patient_birth[current_pid] = birth_year
                break

# Fallback: if no births found, try broader search
if not patient_birth:
    print("No patient births found with initial patterns, trying fallback...")
    # Try to find any birth year patterns in the document
    birth_candidates = re.findall(r'born.*?(\d{4})', patient_md, re.IGNORECASE)
    for i, candidate in enumerate(birth_candidates):
        pid_match = re.search(r'(?:patient|Medical Record Number)\s+(\d+)', patient_md, re.IGNORECASE)
        if pid_match and i == 0:
            patient_birth[pid_match.group(1)] = int(candidate)

# Sanity check: verify we parsed patient data
print(f"Parsed {len(patient_birth)} patient birth years from Patient.md")

# Step 3: identify patient IDs that have at least one CRE > 1.2.
abnormal_pids = set()
for pid, cre_list in lab_data.items():
    for cre_value, _ in cre_list:
        if cre_value > 1.2:
            abnormal_pids.add(pid)
            break

# Sanity check
print(f"Found {len(abnormal_pids)} patients with abnormal creatinine (> 1.2)")

# Step 4: among abnormal_pids, count those whose age = ref_year - birth_year < 70.
count = 0
under_70_count = 0
over_70_count = 0
missing_birth_count = 0

for pid in abnormal_pids:
    if pid in patient_birth:
        age = ref_year - patient_birth[pid]
        if age < 70:
            under_70_count += 1
        else:
            over_70_count += 1
    else:
        missing_birth_count += 1

count = under_70_count

# Final sanity checks
print(f"\n=== FINAL RESULTS ===")
print(f"Total abnormal creatinine patients: {len(abnormal_pids)}")
print(f"Patients under 70: {under_70_count}")
print(f"Patients 70+: {over_70_count}")
print(f"Missing birth year: {missing_birth_count}")
print(f"Reference year used: {ref_year}")
print(f"Final answer: {count}")

pd.DataFrame({"count": [count]}).to_csv(OUT / "prediction.csv", index=False)