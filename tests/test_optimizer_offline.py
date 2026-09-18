import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.optimizer import build_constraints, solve, totals  
from app.schemas import parse_request  

cases = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
matches = 0
for case in cases:
    req = parse_request(json.dumps(case["input"]).encode())
    t0 = time.perf_counter()
    cons = build_constraints(req, case["expected_output"]["directive_interpretation"])
    plan = solve(cons)
    tot = totals(plan, cons["tariff"])
    ms = (time.perf_counter() - t0) * 1000
    ref = case["expected_output"]["total_cost_bdt"]
    ok = abs(tot["total_cost_bdt"] - ref) <= 0.01
    matches += ok
    print(f"{case['id']}: cost {tot['total_cost_bdt']:>10}  ref {ref:>7}  {'MATCH' if ok else 'DIFF '}  {ms:.0f} ms")

print(f"\n{matches}/{len(cases)} exact cost matches")
sys.exit(0 if matches == len(cases) else 1)
