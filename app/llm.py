import json
import os
import re

from openai import OpenAI

TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "8"))

SYSTEM_PROMPT = """You convert campus energy operator notes into structured directives for a 24-hour
(hours 0-23) battery/solar/grid schedule. Return ONLY a JSON object, no prose, no markdown.

Allowed directive_type values and the EXACT structured_adjustment shape for each:
- "solar_reduction":         {"windows": [[start, end]], "factor": number}  factor = fraction of solar that REMAINS (0..1)
- "minimum_battery_reserve": {"windows": [[start, end]], "minimum_energy_kwh": number}
- "no_charge_window":        {"windows": [[start, end]]}
- "no_discharge_window":     {"windows": [[start, end]]}
- "max_grid_window":         {"windows": [[start, end]], "max_grid_kwh": number}
- "no_op":                   null  (note does not change today's 24-hour energy schedule)

Time window rules (do NOT list individual hours; give the start and end clock times as stated):
- start and end are whole hours on a 24-hour clock: midnight=0 (as an end time use 24), noon=12,
  1 PM=13, 6 PM=18, 9 PM=21.
- Copy the stated times: "from 3 PM to 5 PM" -> [[15, 17]]; "noon until 4 PM" -> [[12, 16]];
  "08:00-10:00" -> [[8, 10]]; "from 10 PM until midnight" -> [[22, 24]]; "11 PM to 3 AM" -> [[23, 3]].
- A start time WITHOUT AM/PM takes the AM/PM of the end time: "between 8 and 11 PM" means 8 PM to 11 PM
  -> [[20, 23]] (never [[8, 23]]); "2-4 PM" -> [[14, 16]]; "from 7 to 9 AM" -> [[7, 9]].
- Operator windows are short; a window longer than 12 hours is almost always an AM/PM mistake.
- Bare small numbers in daytime context are afternoon: "from two until four" -> [[14, 16]].
- A single hour ("at 8 PM", "during the 8 PM hour") -> [[20, 21]].
- Several separate periods -> several windows.

Number rules:
- factor is the fraction of solar that REMAINS. If the note says how much is lost/reduced/cut,
  factor = 1 - that amount; if it says how much remains/is usable/is left, factor = that amount.
  Examples: "drop to 30%" -> 0.3; "70% reduction" / "cut by 70%" -> 0.3; "a third of it is lost" -> 0.667;
  "roughly one-tenth of normal" -> 0.1; "half" -> 0.5; "fully unavailable" / "no solar" -> 0.
- Reserve given as a percentage/fraction of capacity -> convert to kWh with capacity_kwh
  (50% of a 200 kWh battery -> 100).
- max_grid_kwh is the per-hour grid import limit in kWh.

Classification rules:
- Charger isolated/offline/unavailable, "do not charge" -> no_charge_window.
- "must not discharge", discharge disabled/blocked, battery cannot supply/power the load -> no_discharge_window.
- "keep at least X in the battery", reserve that must remain -> minimum_battery_reserve.
- Grid import/intake/feeder/transformer/substation limit -> max_grid_window.
- Solar/PV/panels reduced (cleaning, washing, clouds, inverter work, shading) -> solar_reduction.
- Anything else (events, menus, deadlines, notices, other days like "next week"/"next month",
  or changes not expressible with the types above such as demand changes) -> no_op.
  Never invent a new type. Never change demand, solar, tariff or battery parameters.

Output format (one entry per note, same order, note_index starting at 0):
{"interpretations": [{"note_index": 0, "directive_type": "...",
  "structured_adjustment": {"windows": [[start, end]], ...} or null, "explanation": "one short sentence"}]}"""


def _provider(name: str, key_env: str, base_url: str, model: str, extra: dict | None = None):
    key = os.getenv(key_env)
    if not key or not model:
        return None
    return {"name": f"{name}:{model}", "model": model, "extra": extra or {},
            "client": OpenAI(api_key=key, base_url=base_url, timeout=TIMEOUT_S, max_retries=0)}


def _providers() -> list[dict]:
    gemini_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    effort = {"reasoning_effort": os.getenv("GEMINI_REASONING_EFFORT", "low")}
    chain = [
        _provider("gemini", "GEMINI_API_KEY", gemini_url, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"), effort),
        _provider("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1",
                  os.getenv("MISTRAL_MODEL", "ministral-14b-latest")),
        _provider("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1",
                  os.getenv("MISTRAL_FALLBACK_MODEL", "ministral-8b-latest")),
        _provider("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1",
                  os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
        _provider("gemini", "GEMINI_API_KEY", gemini_url, os.getenv("GEMINI_FALLBACK_MODEL", ""), effort),
    ]
    return [p for p in chain if p]


PROVIDERS = _providers()


def call(provider: dict, notes: list[str], battery: dict, feedback: str = "") -> list:
    user = json.dumps({
        "battery": {"capacity_kwh": battery["capacity_kwh"],
                    "minimum_energy_kwh": battery["minimum_energy_kwh"]},
        "operator_notes": [{"note_index": i, "text": n} for i, n in enumerate(notes)],
    })
    if feedback:
        user += f"\n\nYour previous answer was rejected: {feedback}. Return corrected JSON."
    resp = provider["client"].chat.completions.create(
        model=provider["model"],
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        **provider["extra"],
    )
    text = (resp.choices[0].message.content or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in model output")
    data, _ = json.JSONDecoder().raw_decode(text[start:])
    items = data.get("interpretations") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("missing 'interpretations' list")
    return items
