"""Step 8 check: guardrails, rule-based parser, provider chain, cache.
Run from the repo root:  python tests/test_interpreter.py          (no LLM quota)
                         python tests/test_interpreter.py --live   (+1 real Gemini call)
"""
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO if "-v" in sys.argv else logging.ERROR,
                    format="      log: %(message)s")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from app import interpreter, llm
from app.fallback import parse_note
from app.guardrails import GuardrailError, validate_entry

cases = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
failures = 0


def report(ok: bool, text: str):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {text}")


def same(entry: dict, want_type: str, want_adj) -> bool:
    if entry["directive_type"] != want_type or entry["applies"] != (want_type != "no_op"):
        return False
    adj = entry["structured_adjustment"]
    if want_adj is None:
        return adj is None
    return (adj["hours"] == want_adj["hours"] and set(adj) == set(want_adj)
            and all(abs(adj[k] - v) <= 0.01 for k, v in want_adj.items() if k != "hours"))


print("== 1. Guardrails ==")
def rejects(raw, cap=200):
    try:
        validate_entry(raw, cap)
        return False
    except GuardrailError:
        return True

sr = lambda **adj: {"directive_type": "solar_reduction", "structured_adjustment": {"hours": [13, 14], "factor": 0.2, **adj}}
e = validate_entry({"directive_type": "solar_reduction", "applies": False,
                    "structured_adjustment": {"hours": [15, 14, 14.0], "factor": 0.2, "junk": 1}}, 200)
report(e["structured_adjustment"] == {"hours": [14, 15], "factor": 0.2} and e["applies"] is True,
       "hours deduped + sorted, 14.0 -> 14, extra keys dropped, applies forced true")
e = validate_entry({"directive_type": "no_op", "applies": True, "structured_adjustment": {"hours": [1]}}, 200)
report(e["applies"] is False and e["structured_adjustment"] is None and e["explanation"],
       "no_op forced to applies=false + null adjustment + default explanation")
report(rejects({"directive_type": "demand_increase", "structured_adjustment": {"hours": [1]}}), "unsupported type rejected")
report(rejects(sr(factor=20)), "factor 20 rejected (must be 0..1)")
report(rejects(sr(factor=-0.1)), "negative factor rejected")
report(rejects(sr(factor="0.2")), "factor as string rejected")
report(rejects(sr(factor=float("nan"))), "NaN factor rejected")
report(rejects(sr(factor=True)), "bool factor rejected")
report(rejects(sr(hours=[])), "empty hours rejected")
report(rejects(sr(hours=[24])), "hour 24 rejected")
report(rejects(sr(hours=[13.5])), "hour 13.5 rejected")
report(rejects(sr(hours="13-15")), "hours as string rejected")
report(rejects({"directive_type": "minimum_battery_reserve", "structured_adjustment": {"hours": [18], "minimum_energy_kwh": 250}}, 200),
       "reserve above capacity rejected")
report(rejects({"directive_type": "max_grid_window", "structured_adjustment": {"hours": [18], "max_grid_kwh": -1}}),
       "negative grid cap rejected")
report(rejects({"directive_type": "no_charge_window", "structured_adjustment": None}), "missing adjustment rejected")
report(rejects("not an object"), "non-object entry rejected")

win = lambda w: validate_entry({"directive_type": "no_charge_window", "structured_adjustment": {"windows": w}}, 200)["structured_adjustment"]["hours"]
report(win([[17, 19]]) == [17, 18], "window [17,19] -> hours [17,18] (end excluded)")
report(win([[22, 24]]) == [22, 23], "window [22,24] (until midnight) -> [22,23]")
report(win([[22, 0]]) == [22, 23], "window [22,0] (until midnight) -> [22,23]")
report(win([[23, 3]]) == [0, 1, 2, 23], "window [23,3] wraps past midnight -> [0,1,2,23]")
report(win([[19, 20]]) == [19], "single-hour window [19,20] -> [19]")
report(win([[10, 12], [14, 15]]) == [10, 11, 14], "two windows -> [10,11,14]")
report(win([[13.0, 15.0]]) == [13, 14], "window with 13.0/15.0 floats accepted")
report(win([[0, 24]]) == list(range(24)), "window [0,24] -> whole day")
wrej = lambda w: rejects({"directive_type": "no_charge_window", "structured_adjustment": {"windows": w}})
report(wrej([[5, 5]]), "empty window [5,5] rejected")
report(wrej([[24, 2]]), "start 24 rejected")
report(wrej([[13, 25]]), "end 25 rejected")
report(wrej([[13.5, 15]]), "half-hour start rejected")
report(wrej([13, 15]), "flat list instead of pairs rejected")
report(wrej([]), "empty windows list rejected")
report(wrej([[True, 3]]), "bool in window rejected")


print("\n== 2. Rule-based fallback parser (no LLM) ==")
rows = [(n, d["directive_type"], d["structured_adjustment"], c["input"]["battery"]["capacity_kwh"])
        for c in cases for n, d in zip(c["input"]["operator_notes"], c["expected_output"]["directive_interpretation"])]
rows += [(n, t, a, 200) for n, t, a in [
    ("Solar output will drop to about 20% from 1 PM to 3 PM.", "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("Do not charge the battery between 2 PM and 4 PM.", "no_charge_window", {"hours": [14, 15]}),
    ("Keep at least 120 kWh in reserve from 6 PM until 9 PM.", "minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 120}),
    ("The cafeteria menu changes tomorrow.", "no_op", None),
    ("PV production will drop to about 20% between 13:00 and 15:00.", "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("Panel washing from one until three will leave roughly one-fifth of normal solar output.", "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.", "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
]]
fb_ok = 0
for note, t, adj, cap in rows:
    good = same(validate_entry(parse_note(note, cap), cap), t, adj)
    fb_ok += good
    if not good:
        print(f"      miss: {note}")
report(fb_ok == len(rows), f"fallback parser: {fb_ok}/{len(rows)} notes correct")


print("\n== 3. Provider chain (fake providers, no quota) ==")
S6 = cases[5]
NOTES, BAT = S6["input"]["operator_notes"], S6["input"]["battery"]
EXPECTED = [(d["directive_type"], d["structured_adjustment"]) for d in S6["expected_output"]["directive_interpretation"]]
GOOD = [dict(d, explanation="ok") for d in S6["expected_output"]["directive_interpretation"]]


class FakeRateLimit(Exception):
    pass


class FakeTimeout(Exception):
    pass


def scenario(name: str, script: dict, want_calls: dict, want_source_llm=True, clock_step=0.0):
    calls = {p: 0 for p in script}
    feedbacks = []
    clock = [0.0]

    def fake_call(provider, notes, battery, feedback=""):
        pname = provider["name"]
        step = script[pname][min(calls[pname], len(script[pname]) - 1)]
        calls[pname] += 1
        feedbacks.append(feedback)
        clock[0] += clock_step
        if isinstance(step, type) and issubclass(step, Exception):
            raise step("simulated")
        return step() if callable(step) else step

    llm.PROVIDERS = [{"name": p} for p in script]
    llm.call = fake_call
    interpreter._now = lambda: clock[0]
    interpreter._cache.clear()
    result = interpreter.interpret(NOTES, BAT)
    correct = all(same(r, t, a) for r, (t, a) in zip(result, EXPECTED)) and [r["note_index"] for r in result] == [0, 1, 2]
    cached = bool(interpreter._cache)
    ok = correct and calls == want_calls and cached == want_source_llm
    report(ok, f"{name}: calls {calls}, cached={cached}")
    return feedbacks


bad_factor = [dict(GOOD[0], structured_adjustment={"hours": [10, 11], "factor": 50})] + GOOD[1:]
unsupported = [dict(GOOD[0], directive_type="cloud_event")] + GOOD[1:]
scenario("primary answers", {"A": [GOOD], "B": [GOOD]}, {"A": 1, "B": 0})
scenario("primary 429 -> backup answers", {"A": [FakeRateLimit], "B": [GOOD]}, {"A": 1, "B": 1})
scenario("primary timeout -> backup answers", {"A": [FakeTimeout], "B": [GOOD]}, {"A": 1, "B": 1})
scenario("bad JSON -> retry same provider", {"A": [ValueError, GOOD], "B": [GOOD]}, {"A": 2, "B": 0})
fb = scenario("factor 50 -> retry with feedback", {"A": [bad_factor, GOOD], "B": [GOOD]}, {"A": 2, "B": 0})
report("factor" in fb[1], f"retry feedback mentions the problem: {fb[1][:80]!r}")
scenario("unsupported type twice -> backup", {"A": [unsupported, unsupported], "B": [GOOD]}, {"A": 2, "B": 1})
scenario("missing note -> backup fills it", {"A": [GOOD[:2], GOOD[:2]], "B": [GOOD]}, {"A": 2, "B": 1})
scenario("all providers down -> rule parser, not cached",
         {"A": [FakeRateLimit], "B": [FakeTimeout]}, {"A": 1, "B": 1}, want_source_llm=False)
scenario("time budget stops slow chain -> rule parser",
         {"A": [ValueError, ValueError], "B": [GOOD]}, {"A": 2, "B": 0}, want_source_llm=False, clock_step=10.0)

llm.PROVIDERS = []
interpreter._cache.clear()
result = interpreter.interpret(NOTES, BAT)
report(all(same(r, t, a) for r, (t, a) in zip(result, EXPECTED)), "no providers configured -> rule parser still answers")

llm.PROVIDERS, llm.call = [{"name": "A"}], lambda *a, **k: GOOD
interpreter._cache.clear()
interpreter.interpret(NOTES, BAT)
llm.call = lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called despite cache"))
try:
    interpreter.interpret(NOTES, BAT)
    report(True, "second identical request served from cache (0 LLM calls)")
except AssertionError as e:
    report(False, str(e))


if "--live" in sys.argv:
    import importlib
    import time
    print("\n== 4. Live Gemini call through interpret() ==")
    importlib.reload(llm)
    interpreter._now = time.monotonic
    interpreter._cache.clear()
    t0 = time.perf_counter()
    result = interpreter.interpret(NOTES, BAT)
    t1 = time.perf_counter()
    interpreter.interpret(NOTES, BAT)
    t2 = time.perf_counter()
    report(all(same(r, t, a) for r, (t, a) in zip(result, EXPECTED)) and bool(interpreter._cache),
           f"live {llm.PROVIDERS[0]['name'] if llm.PROVIDERS else 'no provider'}: {t1 - t0:.2f} s, cached repeat {t2 - t1:.3f} s")

print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
sys.exit(1 if failures else 0)
