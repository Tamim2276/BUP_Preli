import numpy as np
from scipy.optimize import linprog

H = 24


def build_constraints(req: dict, directives: list[dict]) -> dict:
    bat = req["battery"]
    hours = sorted(req["hours"], key=lambda x: x["hour"])
    c = {
        "demand": [float(h["demand_kwh"]) for h in hours],
        "solar": [float(h["solar_kwh"]) for h in hours],
        "tariff": [float(h["tariff_bdt_per_kwh"]) for h in hours],
        "e_min": [float(bat["minimum_energy_kwh"])] * H,
        "e_max": float(bat["capacity_kwh"]),
        "e0": float(bat["initial_energy_kwh"]),
        "ch_max": [float(bat["max_charge_kwh_per_hour"])] * H,
        "dis_max": [float(bat["max_discharge_kwh_per_hour"])] * H,
        "grid_max": [None] * H,
    }
    for d in directives:
        t, adj = d["directive_type"], d["structured_adjustment"]
        if t == "no_op" or adj is None:
            continue
        for h in adj["hours"]:
            if t == "solar_reduction":
                c["solar"][h] *= adj["factor"]
            elif t == "minimum_battery_reserve":
                c["e_min"][h] = max(c["e_min"][h], adj["minimum_energy_kwh"])
            elif t == "no_charge_window":
                c["ch_max"][h] = 0.0
            elif t == "no_discharge_window":
                c["dis_max"][h] = 0.0
            elif t == "max_grid_window":
                cur = c["grid_max"][h]
                c["grid_max"][h] = adj["max_grid_kwh"] if cur is None else min(cur, adj["max_grid_kwh"])
    return c


def solve(c: dict) -> list[dict]:
    n, G, S, B = 3 * H, 0, H, 2 * H          
    cost = np.zeros(n)
    cost[G:G + H] = c["tariff"]
    cost[S:S + H] = -1e-6                      
    bounds = ([(0, c["grid_max"][h]) for h in range(H)] +
              [(0, c["solar"][h]) for h in range(H)] +
              [(-c["dis_max"][h], c["ch_max"][h]) for h in range(H)])
    A_eq, b_eq = [], []
    for h in range(H):
        row = np.zeros(n)
        row[G + h], row[S + h], row[B + h] = 1, 1, -1
        A_eq.append(row)
        b_eq.append(c["demand"][h])
    row = np.zeros(n)                          
    row[B:B + H] = 1
    A_eq.append(row)
    b_eq.append(0.0)
    A_ub, b_ub = [], []
    for h in range(H):                       
        row = np.zeros(n)
        row[B:B + h + 1] = 1
        A_ub.append(row)
        b_ub.append(c["e_max"] - c["e0"])
        A_ub.append(-row)
        b_ub.append(c["e0"] - c["e_min"][h])
    res = linprog(cost, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs")
    if res.status != 0:
        raise ValueError(f"infeasible: {res.message}")
    return to_plan(c, res.x)


def _clean(v: float) -> float:
    v = round(float(v), 4)                    
    return 0.0 if abs(v) < 1e-6 else v


def to_plan(c: dict, x) -> list[dict]:
    plan, e = [], c["e0"]
    for h in range(H):
        b = _clean(min(max(x[2 * H + h], -c["dis_max"][h]), c["ch_max"][h]))
        s = _clean(min(max(x[H + h], 0.0), c["solar"][h]))
        grid = _clean(c["demand"][h] + b - s)
        if grid < 0:                           
            s, grid = _clean(s + grid), 0.0
        e = _clean(e + b)
        plan.append({
            "hour": h,
            "grid_kwh": grid,
            "solar_used_kwh": s,
            "battery_action": "charge" if b > 0 else "discharge" if b < 0 else "idle",
            "battery_kwh": abs(b),
            "battery_energy_after_kwh": e,
        })
    return plan


def totals(plan: list[dict], tariff: list[float]) -> dict:
    return {
        "total_grid_kwh": round(sum(p["grid_kwh"] for p in plan), 4),
        "total_cost_bdt": round(sum(p["grid_kwh"] * tariff[p["hour"]] for p in plan), 4),
        "peak_grid_kwh": round(max(p["grid_kwh"] for p in plan), 4),
    }
