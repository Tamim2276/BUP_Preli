"""Step 4 check: request validation. Run: python tests/test_schemas.py"""
import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.schemas import RequestError, parse_request  

SAMPLES = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
BASE = SAMPLES[0]["input"]
failures = 0


def expect(name: str, body, status):
    global failures
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    try:
        parse_request(raw)
        got = None
    except RequestError as e:
        got = e.status
    ok = got == status
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:<42} expected {status or 'accept'}, got {got or 'accept'}")


def mutate(fn):
    body = copy.deepcopy(BASE)
    fn(body)
    return body


# Valid inputs
for case in SAMPLES:
    expect(f"{case['id']} valid", case["input"], None)
expect("hours in shuffled order", mutate(lambda b: random.shuffle(b["hours"])), None)
expect("extra unknown field is ignored", mutate(lambda b: b.update(note="x")), None)
shuffled = parse_request(json.dumps(mutate(lambda b: random.shuffle(b["hours"]))).encode())
assert [h["hour"] for h in shuffled["hours"]] == list(range(24)), "hours not sorted"

# 400: malformed / structurally invalid
expect("malformed JSON", b"{bad json", 400)
expect("empty body", b"", 400)
expect("non-UTF-8 bytes", b"\xff\xfe\x00", 400)
expect("JSON array instead of object", [], 400)
expect("NaN literal", json.dumps(BASE).replace('"solar_kwh": 0,', '"solar_kwh": NaN,', 1).encode(), 400)
expect("missing battery", mutate(lambda b: b.pop("battery")), 400)
expect("missing scenario_id", mutate(lambda b: b.pop("scenario_id")), 400)
expect("23 hours", mutate(lambda b: b["hours"].pop()), 400)
expect("duplicate hour", mutate(lambda b: b["hours"][6].update(hour=5)), 400)
expect("hour 24", mutate(lambda b: b["hours"][23].update(hour=24)), 400)
expect("0 notes", mutate(lambda b: b.update(operator_notes=[])), 400)
expect("4 notes", mutate(lambda b: b.update(operator_notes=["a", "b", "c", "d"])), 400)
expect("empty note", mutate(lambda b: b.update(operator_notes=[""])), 400)
expect("whitespace-only note", mutate(lambda b: b.update(operator_notes=["   "])), 400)
expect("note is not a string", mutate(lambda b: b.update(operator_notes=[5])), 400)
expect("negative demand", mutate(lambda b: b["hours"][0].update(demand_kwh=-1)), 400)
expect("demand as string", mutate(lambda b: b["hours"][0].update(demand_kwh="90")), 400)
expect("tariff as bool", mutate(lambda b: b["hours"][0].update(tariff_bdt_per_kwh=True)), 400)
expect("negative charge limit", mutate(lambda b: b["battery"].update(max_charge_kwh_per_hour=-5)), 400)

# 422: well-formed but impossible
expect("initial energy above capacity", mutate(lambda b: b["battery"].update(initial_energy_kwh=999)), 422)
expect("initial energy below minimum", mutate(lambda b: b["battery"].update(initial_energy_kwh=10)), 422)
expect("minimum above capacity", mutate(lambda b: b["battery"].update(minimum_energy_kwh=500)), 422)

print(f"\n{'ALL PASS' if not failures else f'{failures} FAILURE(S)'}")
sys.exit(1 if failures else 0)
