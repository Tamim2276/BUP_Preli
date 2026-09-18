"""Step 6 check: optimizer + replay checker with the EXPECTED directives, no LLM.
Run from the repo root:  python tests/test_optimizer_offline.py
"""
import copy
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.optimizer import build_constraints, solve, totals
from app.schemas import parse_request
from app.validator import check_plan, check_totals

cases = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
failures = 0


def report(ok: bool, text: str):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


# 1. Samples: exact reference cost, our plan valid, totals consistent, reference plan valid
print("== 1. Public samples ==")
built = {}
for case in cases:
    req = parse_request(json.dumps(case["input"]).encode())
    t0 = time.perf_counter()
    cons = build_constraints(req, case["expected_output"]["directive_interpretation"])
    plan = solve(cons)
    tot = totals(plan, cons["tariff"])
    ms = (time.perf_counter() - t0) * 1000
    ref = case["expected_output"]["total_cost_bdt"]
    errs = check_plan(plan, cons) + check_totals(plan, cons["tariff"], tot)
    ref_errs = check_plan(case["expected_output"]["hourly_plan"], cons)
    ok = abs(tot["total_cost_bdt"] - ref) <= 0.01 and not errs and not ref_errs
    report(ok, f"{case['id']}: cost {tot['total_cost_bdt']:>9} ref {ref:>6} | our plan errors {len(errs)} "
               f"| reference plan errors {len(ref_errs)} | {ms:.0f} ms {(errs + ref_errs)[:2]}")
    built[case["id"]] = (plan, cons, tot)


# 2. The checker must catch broken plans
print("\n== 2. Checker catches violations ==")
def tamper(sample_id, fn, expect_text, cons_fn=None):
    plan, cons, tot = built[sample_id]
    plan, cons = copy.deepcopy(plan), copy.deepcopy(cons)
    fn(plan)
    if cons_fn:
        cons_fn(cons)
    errs = check_plan(plan, cons)
    report(any(expect_text in e for e in errs), f"{sample_id}: expect '{expect_text}' -> {errs[:2]}")

def force(p, action, kwh, grid_delta):
    p.update(battery_action=action, battery_kwh=kwh, grid_kwh=p["grid_kwh"] + grid_delta)

tamper("SAMPLE-01", lambda pl: pl[0].update(grid_kwh=pl[0]["grid_kwh"] + 5), "energy balance")
tamper("SAMPLE-01", lambda pl: pl[12].update(solar_used_kwh=180), "solar used")          # factor 0.25 at h12
tamper("SAMPLE-02", lambda pl: force(pl[2], "charge", 10, 10), "charge 10 > limit 0.0")   # no-charge window
tamper("SAMPLE-04", lambda pl: force(pl[18], "discharge", 10, -10), "discharge 10 > limit 0.0")  # no-discharge
tamper("SAMPLE-05", lambda pl: pl[18].update(grid_kwh=200), "> cap 155")                 # grid cap
tamper("SAMPLE-03", lambda pl: None, "outside", lambda c: c["e_min"].__setitem__(19, 999))  # reserve
tamper("SAMPLE-06", lambda pl: pl[0].update(battery_action="idle", battery_kwh=5), "idle but")
tamper("SAMPLE-07", lambda pl: force(pl[23], "idle", 0, -pl[23]["battery_kwh"]), "end-of-day")
_, c8, t8 = built["SAMPLE-08"]
bad = check_totals(built["SAMPLE-08"][0], c8["tariff"], {**t8, "total_cost_bdt": t8["total_cost_bdt"] + 1})
report(bool(bad), f"SAMPLE-08: wrong reported total_cost_bdt caught -> {bad[:1]}")


# 3. Random stress test: decimal inputs + random directives must always give valid plans
print("\n== 3. Random scenarios ==")
random.seed(7)
valid = infeasible = 0
for i in range(300):
    cap = round(random.uniform(100, 400), 3)
    mn = round(random.uniform(0, cap * 0.3), 3)
    req = {
        "hours": [{"hour": h, "demand_kwh": round(random.uniform(50, 250), 3),
                   "solar_kwh": round(max(0.0, random.uniform(-50, 200)), 3),
                   "tariff_bdt_per_kwh": round(random.uniform(3, 35), 3)} for h in range(24)],
        "battery": {"capacity_kwh": cap, "initial_energy_kwh": round(random.uniform(mn, cap), 3),
                    "minimum_energy_kwh": mn,
                    "max_charge_kwh_per_hour": round(random.uniform(10, 80), 3),
                    "max_discharge_kwh_per_hour": round(random.uniform(10, 80), 3)},
    }
    s = random.randint(0, 20)
    dirs = [
        {"directive_type": "solar_reduction", "structured_adjustment": {"hours": list(range(s, s + 3)), "factor": round(random.random(), 3)}},
        {"directive_type": "no_charge_window", "structured_adjustment": {"hours": [random.randint(0, 23)]}},
        {"directive_type": "no_discharge_window", "structured_adjustment": {"hours": [random.randint(0, 23)]}},
        {"directive_type": "max_grid_window", "structured_adjustment": {"hours": [random.randint(0, 23)], "max_grid_kwh": 400}},
        {"directive_type": "minimum_battery_reserve", "structured_adjustment": {"hours": [random.randint(0, 23)], "minimum_energy_kwh": round(random.uniform(mn, cap), 3)}},
        {"directive_type": "no_op", "structured_adjustment": None},
    ]
    cons = build_constraints(req, dirs)
    try:
        plan = solve(cons)
    except ValueError:
        infeasible += 1          # random reserve can be unreachable; that's the LP correctly saying no
        continue
    errs = check_plan(plan, cons) + check_totals(plan, cons["tariff"], totals(plan, cons["tariff"]))
    valid += not errs
    if errs:
        report(False, f"random #{i}: {errs[:3]}")
feasible = 300 - infeasible
report(valid == feasible, f"{valid}/{feasible} feasible random plans valid ({infeasible} infeasible skipped)")

print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
sys.exit(1 if failures else 0)
