import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from app import llm
from app.fallback import parse_note
from app.guardrails import validate_entry
from app.interpreter import _check_all

ITEMS = json.loads((Path(__file__).parent / "paraphrases.json").read_text(encoding="utf-8"))


def correct(entry, item) -> bool:
    if entry is None or entry["directive_type"] != item["type"]:
        return False
    a, b = entry["structured_adjustment"], item["adjustment"]
    if b is None:
        return a is None
    return (a["hours"] == b["hours"] and set(a) == set(b)
            and all(abs(a[k] - v) <= 0.01 for k, v in b.items() if k != "hours"))


def describe(entry) -> str:
    return "rejected by guardrails" if entry is None else f"{entry['directive_type']} {entry['structured_adjustment']}"


def batches():
    by_cap = defaultdict(list)
    for item in ITEMS:
        by_cap[item["capacity"]].append(item)
    for cap, items in by_cap.items():
        for i in range(0, len(items), 3):
            yield cap, items[i:i + 3]


def main():
    rule_ok = sum(correct(validate_entry(parse_note(it["note"], it["capacity"]), it["capacity"]), it) for it in ITEMS)
    print(f"Rule-based fallback parser (no quota): {rule_ok}/{len(ITEMS)}")
    selected = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--provider=")), None)
    chosen = [p for p in llm.PROVIDERS if selected in p["name"]] if selected else llm.PROVIDERS
    if not chosen:
        sys.exit(f"No configured provider matches '{selected or 'any'}'. Is its key in .env?")
    provider = chosen[0]
    print(f"\nLLM {provider['name']} (uses ~{len(list(batches()))} calls):")
    pace = float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--pace=")), 0))
    ok, lat, misses = 0, [], []
    for n, (cap, group) in enumerate(batches()):
        if n and pace:
            time.sleep(pace)
        t0 = time.perf_counter()
        try:
            raw = llm.call(provider, [it["note"] for it in group],
                           {"capacity_kwh": cap, "minimum_energy_kwh": 20})
        except Exception as e:
            print(f"  call failed: {type(e).__name__}: {str(e)[:100]}")
            misses += [(it, "call failed") for it in group]
            continue
        lat.append(time.perf_counter() - t0)
        good, errors = _check_all(raw, len(group), cap)
        for i, it in enumerate(group):
            entry = good.get(i)
            if correct(entry, it):
                ok += 1
            else:
                reason = "; ".join(e for e in errors if f"note {i}" in e or f"[{i}" in e or "entry" in e)
                misses.append((it, describe(entry) + (f" ({reason})" if entry is None else "")))
    for it, got in misses:
        print(f"  MISS: {it['note']}\n        got  {got}\n        want {it['type']} {it['adjustment']}")
    lat.sort()
    print(f"\n{provider['name']}: {ok}/{len(ITEMS)} correct ({100 * ok / len(ITEMS):.0f}%)"
          + (f" | median {lat[len(lat) // 2]:.2f}s | max {lat[-1]:.2f}s" if lat else ""))
    sys.exit(0 if ok >= 0.9 * len(ITEMS) else 1)


if __name__ == "__main__":
    main()
