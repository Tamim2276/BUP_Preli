from dotenv import load_dotenv
load_dotenv()

import logging

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .interpreter import interpret
from .optimizer import build_constraints, solve, totals
from .schemas import ErrorResponse, OptimizeResponse, RequestError, parse_request
from .validator import check_plan, check_totals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gridwise")

app = FastAPI(title="GridWise LLM Optimizer")

EXAMPLE = {
    "scenario_id": "DOCS-EXAMPLE",
    "operator_notes": [
        "Cloud cover during panel inspection will leave about half of the forecast solar output from 10 AM until noon.",
        "The charging circuit will be unavailable from 2 PM until 4 PM.",
        "The library is extending book-return hours next week.",
    ],
    "hours": [
        {"hour": h, "demand_kwh": d, "solar_kwh": s, "tariff_bdt_per_kwh": t}
        for h, (d, s, t) in enumerate(zip(
            [90, 85, 80, 80, 85, 95, 110, 130, 150, 165, 175, 180, 185, 180, 170, 165, 170, 185, 205, 215, 205, 175, 135, 105],
            [0, 0, 0, 0, 0, 0, 5, 20, 50, 90, 130, 160, 180, 170, 140, 90, 45, 10, 0, 0, 0, 0, 0, 0],
            [6, 6, 5, 5, 5, 6, 8, 10, 12, 14, 16, 16, 15, 14, 13, 14, 18, 22, 28, 30, 26, 18, 10, 7]))
    ],
    "battery": {"capacity_kwh": 220, "initial_energy_kwh": 110, "minimum_energy_kwh": 40,
                "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50},
}


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error: %s", type(exc).__name__)
    return error(500, "internal error")


def plan_summary(directives: list[dict], tot: dict) -> str:
    applied = [d["directive_type"] for d in directives if d["applies"]]
    ignored = len(directives) - len(applied)
    head = f"Applied {len(applied)} operator directive(s)"
    if applied:
        head += f" ({', '.join(applied)})"
    return (f"{head} and ignored {ignored} irrelevant note(s). The battery charges in cheap or "
            f"solar-rich hours and discharges in expensive hours, returning to its initial level; "
            f"total grid cost {tot['total_cost_bdt']:.2f} BDT.")


def pipeline(req: dict) -> dict:
    directives = interpret(req["operator_notes"], req["battery"])
    cons = build_constraints(req, directives)
    plan = solve(cons)
    tot = totals(plan, cons["tariff"])
    problems = check_plan(plan, cons) + check_totals(plan, cons["tariff"], tot)
    if problems:
        raise RuntimeError(f"plan failed final replay: {problems[:3]}")
    return {
        "scenario_id": req["scenario_id"],
        "directive_interpretation": directives,
        "hourly_plan": plan,
        "total_grid_kwh": tot["total_grid_kwh"],
        "total_cost_bdt": tot["total_cost_bdt"],
        "peak_grid_kwh": tot["peak_grid_kwh"],
        "plan_summary": plan_summary(directives, tot),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/optimize-energy",
          responses={
              200: {"model": OptimizeResponse, "description": "Interpretation + optimal 24-hour plan"},
              400: {"model": ErrorResponse, "description": "Malformed JSON or structurally invalid request"},
              422: {"model": ErrorResponse, "description": "Semantically invalid or infeasible scenario"},
              500: {"model": ErrorResponse, "description": "Controlled internal error"},
          },
          openapi_extra={"requestBody": {
              "required": True,
              "content": {"application/json": {"schema": {"type": "object"}, "example": EXAMPLE}},
          }})
async def optimize_energy(request: Request):
    try:
        req = parse_request(await request.body())
    except RequestError as e:
        return error(e.status, e.message)
    try:
        return await run_in_threadpool(pipeline, req)
    except ValueError as e:
        if str(e).startswith("infeasible"):
            log.warning("scenario %s infeasible under interpreted directives", req["scenario_id"])
            return error(422, "no feasible schedule under the interpreted directives")
        log.error("pipeline error for %s: %s", req["scenario_id"], type(e).__name__)
        return error(500, "internal error")
    except Exception as e:
        log.error("pipeline error for %s: %s %s", req["scenario_id"], type(e).__name__, str(e)[:200])
        return error(500, "internal error")
