"""Step 7 check: real LLM calls on the sample notes + paraphrases. USES GEMINI QUOTA (~14 calls).
Run from the repo root:  python tests/test_llm.py
"""
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")                     # must run before app.llm builds the provider chain
from app import llm  # noqa: E402
from app.guardrails import GuardrailError, validate_entry  # noqa: E402

cases = json.loads((Path(__file__).parent / "public_samples.json").read_text(encoding="utf-8"))["cases"]
REQUESTS = [(c["id"], c["input"]["operator_notes"], c["input"]["battery"],
             [(d["directive_type"], d["structured_adjustment"]) for d in c["expected_output"]["directive_interpretation"]])
            for c in cases]
BAT200 = {"capacity_kwh": 200, "minimum_energy_kwh": 30}
REQUESTS += [
    ("PARA-1", ["PV production will drop to about 20% between 13:00 and 15:00.",
                "Panel washing from one until three will leave roughly one-fifth of normal solar output.",
                "Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window."], BAT200,
     [("solar_reduction", {"hours": [13, 14], "factor": 0.2})] * 3),
    ("PARA-2", ["The charger is offline between 11:00 and 13:00.",
                "Hold a quarter of the battery's capacity in reserve from 7 PM until 10 PM.",
                "The utility asks us to cap imports at 150 kWh per hour between 18:00 and 21:00."], BAT200,
     [("no_charge_window", {"hours": [11, 12]}),
      ("minimum_battery_reserve", {"hours": [19, 20, 21], "minimum_energy_kwh": 50}),
      ("max_grid_window", {"hours": [18, 19, 20], "max_grid_kwh": 150})]),
    ("PARA-3", ["Solar panels will be cleaned next week.", "Auditorium demand will rise tonight.",
                "Battery discharging is blocked from 5 PM to 7 PM for relay checks."], BAT200,
     [("no_op", None), ("no_op", None), ("no_discharge_window", {"hours": [17, 18]})]),
]


def matches(got, want_type: str, want_adj, capacity: float) -> bool:
    try:
        entry = validate_entry(got, capacity)
    except GuardrailError:
        return False
    if entry["directive_type"] != want_type:
        return False
    adj = entry["structured_adjustment"]
    if want_adj is None:
        return adj is None
    return adj["hours"] == want_adj["hours"] and all(
        abs(adj[k] - v) <= 0.01 for k, v in want_adj.items() if k != "hours")


def run(provider: dict, requests: list) -> tuple[int, int, list[float]]:
    ok = total = 0
    lat = []
    pace = float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--pace=")), 0))
    for n, (rid, notes, battery, expected) in enumerate(requests):
        if n and pace:
            time.sleep(pace)
        t0 = time.perf_counter()
        try:
            items = llm.call(provider, notes, battery)
        except Exception as e:                      # show the error type only, never the key
            print(f"  {rid}: ERROR {type(e).__name__}: {str(e)[:100]}")
            total += len(notes)
            continue
        lat.append(time.perf_counter() - t0)
        for i, (t, adj) in enumerate(expected):
            total += 1
            got = items[i] if i < len(items) else None
            good = matches(got, t, adj, battery["capacity_kwh"])
            ok += good
            if not good:
                print(f"  {rid} note {i}: MISS\n     note: {notes[i]}\n     got:  {got}\n     want: {t} {adj}")
        print(f"  {rid}: {len(items)} entries, {lat[-1]:.2f} s")
    return ok, total, lat


if not llm.PROVIDERS:
    sys.exit("No provider configured: set GEMINI_API_KEY in .env")
print("Provider chain:", " -> ".join(p["name"] for p in llm.PROVIDERS), "-> rule parser")

selected = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--provider=")), None)
chosen = [p for p in llm.PROVIDERS if selected in p["name"]] if selected else llm.PROVIDERS
if not chosen:
    sys.exit(f"No configured provider matches '{selected}'. Is its key in .env?")
primary = chosen[0]
print(f"\n== Primary {primary['name']}: 10 samples + 3 paraphrase packs ==")
ok, total, lat = run(primary, REQUESTS)
lat.sort()
p95 = lat[max(0, round(0.95 * len(lat)) - 1)] if lat else float("nan")
print(f"\n{primary['name']}: {ok}/{total} notes correct | median {lat[len(lat) // 2] if lat else float('nan'):.2f} s"
      f" | p95 {p95:.2f} s")

for p in ([] if selected else llm.PROVIDERS[1:]):
    print(f"\n== Backup {p['name']}: 1 sample (reachability check) ==")
    b_ok, b_total, _ = run(p, REQUESTS[5:6])
    print(f"{p['name']}: {b_ok}/{b_total} notes correct")

sys.exit(0 if ok == total else 1)
