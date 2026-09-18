import json
import statistics
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.optimizer import build_constraints
from app.validator import check_plan, check_totals

TOP = ["scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh",
       "total_cost_bdt", "peak_grid_kwh", "plan_summary"]
DIR_KEYS = {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}
HOUR_KEYS = {"hour", "grid_kwh", "solar_used_kwh", "battery_action", "battery_kwh", "battery_energy_after_kwh"}


def schema_ok(body: dict, request: dict) -> bool:
    if list(body) != TOP or body["scenario_id"] != request["scenario_id"]:
        return False
    d = body["directive_interpretation"]
    return ([x.get("note_index") for x in d] == list(range(len(request["operator_notes"])))
            and all(set(x) == DIR_KEYS for x in d)
            and len(body["hourly_plan"]) == 24
            and all(set(x) == HOUR_KEYS for x in body["hourly_plan"]))


def entry_ok(got: dict, want: dict) -> bool:
    if got["directive_type"] != want["directive_type"] or got["applies"] != want["applies"]:
        return False
    a, b = got["structured_adjustment"], want["structured_adjustment"]
    if b is None:
        return a is None
    return (isinstance(a, dict) and a.get("hours") == b["hours"] and set(a) == set(b)
            and all(abs(a[k] - v) <= 0.01 for k, v in b.items() if k != "hours"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = (args[0] if args else "http://127.0.0.1:8000").rstrip("/")
    cases = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]

    try:
        h = requests.get(f"{base}/health", timeout=60)
        print(f"GET {base}/health -> {h.status_code} {h.text.strip()}")
    except requests.RequestException as e:
        sys.exit(f"Cannot reach {base}: {type(e).__name__}. Is the server running?")

    print(f"\n{'case':<10} {'HTTP':>4} {'time':>7}  {'schema':<6} {'interp':<6} {'valid':<5} "
          f"{'optimal':<7} cost / reference")
    lat, n_schema, n_interp, n_valid, n_opt = [], 0, 0, 0, 0
    for case in cases:
        req, exp = case["input"], case["expected_output"]
        t0 = time.perf_counter()
        try:
            r = requests.post(f"{base}/optimize-energy", json=req, timeout=35)
        except requests.RequestException as e:
            print(f"{case['id']:<10} FAIL {type(e).__name__}")
            continue
        sec = time.perf_counter() - t0
        lat.append(sec)
        if r.status_code != 200:
            print(f"{case['id']:<10} {r.status_code:>4} {sec:>6.2f}s  {r.text[:80]}")
            continue
        body = r.json()
        s_ok = schema_ok(body, req)
        i_ok = s_ok and all(entry_ok(g, w) for g, w in zip(body["directive_interpretation"],
                                                             exp["directive_interpretation"]))
        cons = build_constraints(req, exp["directive_interpretation"])
        errs = (check_plan(body["hourly_plan"], cons) + check_totals(body["hourly_plan"], cons["tariff"], body)
                if s_ok else ["bad schema"])
        v_ok = not errs
        o_ok = v_ok and abs(body["total_cost_bdt"] - exp["total_cost_bdt"]) <= 0.01
        n_schema += s_ok
        n_interp += i_ok
        n_valid += v_ok
        n_opt += o_ok
        mark = lambda ok: "ok" if ok else "FAIL"
        print(f"{case['id']:<10} {r.status_code:>4} {sec:>6.2f}s  {mark(s_ok):<6} {mark(i_ok):<6} {mark(v_ok):<5} "
              f"{mark(o_ok):<7} {body.get('total_cost_bdt')} / {exp['total_cost_bdt']}")
        if not i_ok and s_ok:
            for g, w in zip(body["directive_interpretation"], exp["directive_interpretation"]):
                if not entry_ok(g, w):
                    print(f"{'':<12}note {g['note_index']}: got {g['directive_type']} {g['structured_adjustment']}"
                          f" | want {w['directive_type']} {w['structured_adjustment']}")
        if errs and s_ok:
            print(f"{'':<12}{errs[:3]}")

    n = len(cases)
    lat.sort()
    p95 = lat[max(0, round(0.95 * len(lat)) - 1)] if lat else float("nan")
    print(f"\nschema {n_schema}/{n} | interpretation {n_interp}/{n} | valid {n_valid}/{n} | optimal {n_opt}/{n}")
    if lat:
        print(f"latency: median {statistics.median(lat):.2f}s | p95 {p95:.2f}s | max {max(lat):.2f}s"
              f"   (judge: full points if p95 <= 5s, hard limit 30s)")
    passed = n_schema == n_interp == n_valid == n_opt == n
    print("ALL PASS" if passed else "SOME CHECKS FAILED")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
