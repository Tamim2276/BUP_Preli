import math

REQUIRED = {
    "solar_reduction": ("factor",),
    "minimum_battery_reserve": ("minimum_energy_kwh",),
    "no_charge_window": (),
    "no_discharge_window": (),
    "max_grid_window": ("max_grid_kwh",),
    "no_op": None,
}
NO_OP_TEXT = "This note does not affect today's 24-hour energy schedule."


class GuardrailError(ValueError):
    pass


def _number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GuardrailError(f"{name} must be a finite number")
    return float(value)


def _hours(value) -> list[int]:
    if not isinstance(value, list) or not value:
        raise GuardrailError("hours must be a non-empty list")
    out = set()
    for h in value:
        if isinstance(h, float) and h.is_integer():
            h = int(h)
        if isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23:
            raise GuardrailError(f"invalid hour {h!r}, hours must be integers 0-23")
        out.add(h)
    return sorted(out)


def _clock(value, name: str, low: int, high: int) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise GuardrailError(f"window {name} {value!r} must be a whole clock hour {low}-{high}")
    return value


def _windows(value) -> list[int]:
    if not isinstance(value, list) or not value:
        raise GuardrailError("windows must be a non-empty list of [start, end] pairs")
    out = set()
    for w in value:
        if not isinstance(w, (list, tuple)) or len(w) != 2:
            raise GuardrailError(f"window {w!r} must be a [start, end] pair")
        start, end = _clock(w[0], "start", 0, 23), _clock(w[1], "end", 0, 24)
        if start == end:
            raise GuardrailError(f"window {w!r} is empty: end must differ from start")
        out.update(range(start, end) if end > start else [*range(start, 24), *range(0, end)])
    return sorted(out)


def validate_entry(raw, capacity: float) -> dict:
    if not isinstance(raw, dict):
        raise GuardrailError("entry must be an object")
    t = raw.get("directive_type")
    if t not in REQUIRED:
        raise GuardrailError(f"unsupported directive_type {t!r}")
    explanation = str(raw.get("explanation") or "").strip()[:300]
    if t == "no_op":
        return {"applies": False, "directive_type": "no_op", "structured_adjustment": None,
                "explanation": explanation or NO_OP_TEXT}
    adj = raw.get("structured_adjustment")
    if not isinstance(adj, dict):
        raise GuardrailError(f"{t} needs a structured_adjustment object")
    clean = {"hours": _windows(adj["windows"]) if "windows" in adj else _hours(adj.get("hours"))}
    if t == "solar_reduction":
        factor = _number(adj.get("factor"), "factor")
        if not 0 <= factor <= 1:
            raise GuardrailError("factor must be between 0 and 1 (the fraction of solar that remains)")
        clean["factor"] = factor
    elif t == "minimum_battery_reserve":
        reserve = _number(adj.get("minimum_energy_kwh"), "minimum_energy_kwh")
        if not 0 <= reserve <= capacity:
            raise GuardrailError(f"minimum_energy_kwh must be between 0 and capacity {capacity}")
        clean["minimum_energy_kwh"] = reserve
    elif t == "max_grid_window":
        cap = _number(adj.get("max_grid_kwh"), "max_grid_kwh")
        if cap < 0:
            raise GuardrailError("max_grid_kwh must be >= 0")
        clean["max_grid_kwh"] = cap
    return {"applies": True, "directive_type": t, "structured_adjustment": clean,
            "explanation": explanation or f"Interpreted as {t}."}
