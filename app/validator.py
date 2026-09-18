TOL = 0.01


def check_plan(plan: list[dict], c: dict) -> list[str]:
    if [p.get("hour") for p in plan] != list(range(24)):
        return ["hourly_plan must contain hours 0..23 in order"]
    errs = []
    e = c["e0"]
    for p in plan:
        h = p["hour"]
        g, s, a, k = p["grid_kwh"], p["solar_used_kwh"], p["battery_action"], p["battery_kwh"]
        if a not in ("charge", "discharge", "idle"):
            errs.append(f"h{h}: invalid battery_action {a!r}")
            continue
        ch = k if a == "charge" else 0.0
        dis = k if a == "discharge" else 0.0
        if min(g, s, k, p["battery_energy_after_kwh"]) < -TOL:
            errs.append(f"h{h}: negative value")
        if s > c["solar"][h] + TOL:
            errs.append(f"h{h}: solar used {s} > effective solar {c['solar'][h]}")
        if a == "idle" and abs(k) > TOL:
            errs.append(f"h{h}: idle but battery_kwh = {k}")
        if ch > c["ch_max"][h] + TOL:
            errs.append(f"h{h}: charge {ch} > limit {c['ch_max'][h]}")
        if dis > c["dis_max"][h] + TOL:
            errs.append(f"h{h}: discharge {dis} > limit {c['dis_max'][h]}")
        if c["grid_max"][h] is not None and g > c["grid_max"][h] + TOL:
            errs.append(f"h{h}: grid {g} > cap {c['grid_max'][h]}")
        if abs(g + s + dis - c["demand"][h] - ch) > TOL:
            errs.append(f"h{h}: energy balance broken")
        e += ch - dis
        if abs(e - p["battery_energy_after_kwh"]) > TOL:
            errs.append(f"h{h}: battery_energy_after_kwh {p['battery_energy_after_kwh']} != replayed {e:.4f}")
        if e < c["e_min"][h] - TOL or e > c["e_max"] + TOL:
            errs.append(f"h{h}: battery level {e:.4f} outside [{c['e_min'][h]}, {c['e_max']}]")
    if abs(e - c["e0"]) > TOL:
        errs.append(f"end-of-day battery {e:.4f} != initial {c['e0']}")
    return errs


def check_totals(plan: list[dict], tariff: list[float], reported: dict) -> list[str]:
    want = {
        "total_grid_kwh": sum(p["grid_kwh"] for p in plan),
        "total_cost_bdt": sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan),
        "peak_grid_kwh": max(p["grid_kwh"] for p in plan),
    }
    return [f"{k}: reported {reported.get(k)} != recalculated {v:.4f}"
            for k, v in want.items()
            if not isinstance(reported.get(k), (int, float)) or abs(reported[k] - v) > TOL]
