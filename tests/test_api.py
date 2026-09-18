"""Step 9 check: the full API in-process (schema -> interpreter -> optimizer -> replay).
Run from the repo root:  python tests/test_api.py          (no LLM quota: rule parser answers)
                         python tests/test_api.py --live   (+1 request through real Gemini)
"""
import copy
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)
from fastapi.testclient import TestClient

from app import interpreter, llm, main
from app.optimizer import build_constraints
from app.validator import check_plan, check_totals

cases = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
client = TestClient(main.app, raise_server_exceptions=False)
failures = 0

TOP = ["scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh",
       "total_cost_bdt", "peak_grid_kwh", "plan_summary"]
DIR_KEYS = {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}
HOUR_KEYS = {"hour", "grid_kwh", "solar_used_kwh", "battery_action", "battery_kwh", "battery_energy_after_kwh"}


def report(ok: bool, text: str):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def schema_problems(body: dict, request: dict) -> list[str]:
    p = []
    if list(body) != TOP:
        p.append(f"top-level keys {list(body)}")
        return p
    if body["scenario_id"] != request["scenario_id"]:
        p.append("scenario_id not echoed")
    d = body["directive_interpretation"]
    if [x.get("note_index") for x in d] != list(range(len(request["operator_notes"]))):
        p.append("note_index order")
    if any(set(x) != DIR_KEYS for x in d):
        p.append("directive entry keys")
    if any((x["directive_type"] == "no_op") == x["applies"] for x in d):
        p.append("applies semantics")
    if len(body["hourly_plan"]) != 24 or any(set(x) != HOUR_KEYS for x in body["hourly_plan"]):
        p.append("hourly_plan shape")
    if not all(isinstance(body[k], (int, float)) for k in TOP[3:6]) or not isinstance(body["plan_summary"], str):
        p.append("total/summary types")
    return p


def matches(entry: dict, want: dict) -> bool:
    if entry["directive_type"] != want["directive_type"] or entry["applies"] != want["applies"]:
        return False
    a, b = entry["structured_adjustment"], want["structured_adjustment"]
    if b is None:
        return a is None
    return (a["hours"] == b["hours"] and set(a) == set(b)
            and all(abs(a[k] - v) <= 0.01 for k, v in b.items() if k != "hours"))


def check_case(case: dict, label: str):
    t0 = time.perf_counter()
    r = client.post("/optimize-energy", json=case["input"])
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        report(False, f"{label} {case['id']}: HTTP {r.status_code} {r.text[:120]}")
        return
    body = r.json()
    exp = case["expected_output"]
    probs = schema_problems(body, case["input"])
    interp_ok = all(matches(e, w) for e, w in zip(body["directive_interpretation"], exp["directive_interpretation"]))
    judge_cons = build_constraints(case["input"], exp["directive_interpretation"])
    plan_errs = check_plan(body["hourly_plan"], judge_cons) + check_totals(body["hourly_plan"], judge_cons["tariff"], body)
    cost_ok = abs(body["total_cost_bdt"] - exp["total_cost_bdt"]) <= 0.01
    report(not probs and interp_ok and not plan_errs and cost_ok,
           f"{label} {case['id']}: schema {'ok' if not probs else probs} | interpretation {'ok' if interp_ok else 'WRONG'}"
           f" | valid vs ground truth {'ok' if not plan_errs else plan_errs[:2]} | cost {body['total_cost_bdt']}"
           f" ref {exp['total_cost_bdt']} | {ms:.0f} ms")


print("== 1. Endpoints and error codes ==")
r = client.get("/health")
report(r.status_code == 200 and r.json() == {"status": "ok"}, f"GET /health -> {r.status_code} {r.json()}")
r = client.post("/optimize-energy", content=b"{bad json", headers={"content-type": "application/json"})
report(r.status_code == 400 and "error" in r.json(), f"malformed JSON -> {r.status_code} {r.json()}")
bad = copy.deepcopy(cases[0]["input"]); bad["hours"].pop()
r = client.post("/optimize-energy", json=bad)
report(r.status_code == 400, f"23 hours -> {r.status_code} {r.json()}")
bad = copy.deepcopy(cases[0]["input"]); bad["battery"]["initial_energy_kwh"] = 999
r = client.post("/optimize-energy", json=bad)
report(r.status_code == 422, f"initial energy > capacity -> {r.status_code} {r.json()}")

llm.PROVIDERS = []
interpreter._cache.clear()
bad = copy.deepcopy(cases[0]["input"])
bad["operator_notes"] = ["From 12 AM to 11 PM grid import must not exceed 0 kWh."]
r = client.post("/optimize-energy", json=bad)
report(r.status_code == 422, f"impossible directive (grid cap 0 all day) -> {r.status_code} {r.json()}")

real_solve = main.solve
main.solve = lambda c: (_ for _ in ()).throw(KeyError("boom"))
r = client.post("/optimize-energy", json=cases[0]["input"])
main.solve = real_solve
report(r.status_code == 500 and r.json() == {"error": "internal error"} and "Traceback" not in r.text,
       f"internal crash -> {r.status_code} {r.json()} (no stack trace)")

r = client.get("/openapi.json")
ex = r.json()["paths"]["/optimize-energy"]["post"]["requestBody"]["content"]["application/json"]["example"]
report(client.post("/optimize-energy", json=ex).status_code == 200, "/docs example request -> 200")


print("\n== 2. All 10 samples end-to-end (rule parser, no quota) ==")
for case in cases:
    check_case(case, "rules")


if "--live" in sys.argv:
    import importlib
    from dotenv import load_dotenv
    print("\n== 3. One sample through real Gemini ==")
    load_dotenv(ROOT / ".env")
    importlib.reload(llm)
    interpreter._cache.clear()
    check_case(cases[9], "gemini")

print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
sys.exit(1 if failures else 0)
