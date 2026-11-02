#!/usr/bin/env python3
"""Edit-and-rerun cases for the nse-data-integrity-engine gate_check."""
import sys, os, json, copy

# Add the skill's scripts folder to sys.path
SKILL_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir,
    ".agents", "skills", "nse-data-integrity-engine"
)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

import gate_check

# Load SAMPLE_CLEAN from sample_input.json
INPUT_PATH = os.path.join(SKILL_DIR, "assets", "sample_input.json")
with open(INPUT_PATH, encoding="utf-8") as f:
    all_stocks = json.load(f)["stocks"]

base = None
for s in all_stocks:
    if s["symbol"] == "SAMPLE_CLEAN":
        base = s
        break
assert base is not None, "SAMPLE_CLEAN not found in sample_input.json"


def run_case(label, stock):
    result = gate_check.evaluate(stock)
    status = result["status"]
    confidence = result["data_confidence"]["score"]
    cmp_status = result["validation"]["cmp"]["status"]
    print(f"{label}: status={status}, confidence={confidence}, cmp_status={cmp_status}")
    return status, confidence, cmp_status


# --- E1: twelve_data CMP = 2560, keep all 3 records ---
e1 = copy.deepcopy(base)
for rec in e1["evidence"]["cmp"]:
    if rec["source"] == "twelve_data":
        rec["value"] = 2560
result = run_case("E1", e1)

# --- E2: CMP = only nse_bhavcopy + twelve_data at 2560 ---
e2 = copy.deepcopy(base)
e2["evidence"]["cmp"] = [
    r for r in e2["evidence"]["cmp"] if r["source"] == "nse_bhavcopy"
] + [{"source": "twelve_data", "value": 2560, "as_of": base["trading_date"]}]
result = run_case("E2", e2)

# --- E3: Remove nse_bhavcopy from CMP, leave twelve_data + alpha_vantage ---
e3 = copy.deepcopy(base)
e3["evidence"]["cmp"] = [
    r for r in e3["evidence"]["cmp"] if r["source"] != "nse_bhavcopy"
]
result = run_case("E3", e3)

# --- E4: Set technical score to 74 ---
e4 = copy.deepcopy(base)
e4["scores"]["technical"] = 74
result = run_case("E4", e4)

# --- E5: Set fundamentals_meta.age_days to 45 ---
e5 = copy.deepcopy(base)
e5["evidence"]["fundamentals_meta"]["age_days"] = 45
result = run_case("E5", e5)

# --- E6: Set history.adjusted to false ---
e6 = copy.deepcopy(base)
e6["evidence"]["history"]["adjusted"] = False
result = run_case("E6", e6)

# --- E7: Set rsi14 vendor (twelve_data) value to 70 ---
e7 = copy.deepcopy(base)
for v in e7["evidence"]["indicators"]["rsi14"]["vendors"]:
    if v["source"] == "twelve_data":
        v["value"] = 70
result = run_case("E7", e7)
