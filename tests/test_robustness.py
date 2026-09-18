import copy
import importlib
import json
import logging
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.optimizer import build_constraints
from app.validator import check_plan, check_totals

CASES = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
failures = 0


def report(ok: bool, text: str):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def interp_ok(body: dict, expected: list) -> bool:
    for got, want in zip(body["directive_interpretation"], expected):
        if got["directive_type"] != want["directive_type"]:
            return False
        a, b = got["structured_adjustment"], want["structured_adjustment"]
        if b is None:
            if a is not None:
                return False
        elif a.get("hours") != b["hours"] or any(abs(a[k] - v) > 0.01 for k, v in b.items() if k != "hours"):
            return False
    return True


def bad_inputs(base: str):
    print("== 1. Bad input against the running server ==")
    good = CASES[0]["input"]

    def mutate(fn):
        b = copy.deepcopy(good)
        fn(b)
        return b

    tests = [
        ("malformed JSON", b"{bad json", 400),
        ("empty body", b"", 400),
        ("JSON array", b"[]", 400),
        ("NaN value", json.dumps(good).replace('"solar_kwh": 0,', '"solar_kwh": NaN,', 1).encode(), 400),
        ("missing battery", mutate(lambda b: b.pop("battery")), 400),
        ("23 hours", mutate(lambda b: b["hours"].pop()), 400),
        ("duplicate hour", mutate(lambda b: b["hours"][6].update(hour=5)), 400),
        ("4 notes", mutate(lambda b: b.update(operator_notes=["a", "b", "c", "d"])), 400),
        ("empty note", mutate(lambda b: b.update(operator_notes=[""])), 400),
        ("negative demand", mutate(lambda b: b["hours"][0].update(demand_kwh=-5)), 400),
        ("demand as string", mutate(lambda b: b["hours"][0].update(demand_kwh="90")), 400),
        ("initial energy > capacity", mutate(lambda b: b["battery"].update(initial_energy_kwh=999)), 422),
    ]
    for name, body, want in tests:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        r = requests.post(f"{base}/optimize-energy", data=data,
                          headers={"Content-Type": "application/json"}, timeout=35)
        ok = r.status_code == want and "error" in r.json() and "Traceback" not in r.text
        report(ok, f"{name:<26} -> {r.status_code} {r.text[:70]}")
    r = requests.get(f"{base}/health", timeout=10)
    report(r.status_code == 200, f"{'server still healthy':<26} -> {r.status_code} {r.text}")


def burst(base: str, n: int):
    print(f"\n== 2. Burst: {n} concurrent distinct requests (free tier allows 15/min per model) ==")
    case = CASES[5]
    expected = case["expected_output"]["directive_interpretation"]

    def one(i: int):
        req = copy.deepcopy(case["input"])
        req["scenario_id"] = f"BURST-{i}"
        req["battery"]["capacity_kwh"] += i + 1
        t0 = time.perf_counter()
        try:
            r = requests.post(f"{base}/optimize-energy", json=req, timeout=35)
        except requests.RequestException as e:
            return i, None, time.perf_counter() - t0, type(e).__name__, req
        return i, r, time.perf_counter() - t0, None, req

    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(one, range(n)))
    wall = time.perf_counter() - t_start
    lat = sorted(sec for _, r, sec, _, _ in results if r is not None)
    statuses, n_interp, n_valid = {}, 0, 0
    for i, r, sec, err, req in results:
        key = err or r.status_code
        statuses[key] = statuses.get(key, 0) + 1
        if r is not None and r.status_code == 200:
            body = r.json()
            n_interp += interp_ok(body, expected)
            cons = build_constraints(req, expected)
            n_valid += not (check_plan(body["hourly_plan"], cons) + check_totals(body["hourly_plan"], cons["tariff"], body))
    p95 = lat[max(0, round(0.95 * len(lat)) - 1)] if lat else float("nan")
    report(statuses.get(200, 0) == n, f"HTTP status counts: {statuses}")
    report(n_valid == n, f"plans valid vs ground truth: {n_valid}/{n}")
    report(max(lat, default=99) < 30, f"latency median {statistics.median(lat):.2f}s | p95 {p95:.2f}s | "
                                      f"max {max(lat):.2f}s | wall {wall:.1f}s (hard limit 30s)")
    print(f"      interpretation correct: {n_interp}/{n}  (info: rate-limited requests fall back to the 2nd "
          f"Gemini model, then the rule parser; see server log)")


def wrong_key():
    print("\n== 3. Invalid API key (in-process, no quota) ==")
    logging.disable(logging.CRITICAL)
    os.environ["GEMINI_API_KEY"] = "invalid-key-for-testing"
    os.environ["GROQ_API_KEY"] = ""
    from fastapi.testclient import TestClient
    from app import interpreter, llm, main
    importlib.reload(llm)
    interpreter._cache.clear()
    case = CASES[5]
    t0 = time.perf_counter()
    r = TestClient(main.app).post("/optimize-energy", json=case["input"])
    sec = time.perf_counter() - t0
    ok = r.status_code == 200 and interp_ok(r.json(), case["expected_output"]["directive_interpretation"])
    report(ok, f"Gemini rejects the key -> still HTTP {r.status_code}, answered by rule parser in {sec:.2f}s")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = (args[0] if args else "http://127.0.0.1:8000").rstrip("/")
    n = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--burst=")), 20))
    try:
        requests.get(f"{base}/health", timeout=10)
    except requests.RequestException:
        sys.exit(f"Cannot reach {base}. Start the server first: uvicorn app.main:app --port 8000")
    bad_inputs(base)
    if "--no-burst" not in sys.argv:
        burst(base, n)
    wrong_key()
    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
