"""
  400 = malformed JSON or structurally invalid request
  422 = well-formed but semantically impossible (battery state out of range)
"""
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

NonNeg = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Hour(BaseModel):
    model_config = ConfigDict(strict=True)
    hour: Annotated[int, Field(ge=0, le=23)]
    demand_kwh: NonNeg
    solar_kwh: NonNeg
    tariff_bdt_per_kwh: NonNeg


class Battery(BaseModel):
    model_config = ConfigDict(strict=True)
    capacity_kwh: NonNeg
    initial_energy_kwh: NonNeg
    minimum_energy_kwh: NonNeg
    max_charge_kwh_per_hour: NonNeg
    max_discharge_kwh_per_hour: NonNeg


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(strict=True)
    scenario_id: Annotated[str, Field(min_length=1)]
    operator_notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    hours: Annotated[list[Hour], Field(min_length=24, max_length=24)]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def notes_not_blank(cls, notes: list[str]) -> list[str]:
        if any(not n.strip() for n in notes):
            raise ValueError("operator_notes must be non-empty strings")
        return notes

    @model_validator(mode="after")
    def each_hour_once(self):
        if sorted(h.hour for h in self.hours) != list(range(24)):
            raise ValueError("hours must contain each hour 0..23 exactly once")
        return self


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")      

def parse_request(raw: bytes) -> dict:
    """Raw body -> validated request dict with hours sorted 0..23. Raises RequestError."""
    try:
        body = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):            
        raise RequestError(400, "malformed JSON")
    if not isinstance(body, dict):
        raise RequestError(400, "request body must be a JSON object")
    try:
        req = OptimizeRequest.model_validate(body).model_dump()
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "body"
        raise RequestError(400, f"invalid request: {where}: {first['msg']}")
    req["hours"].sort(key=lambda h: h["hour"])
    b = req["battery"]
    if b["minimum_energy_kwh"] > b["capacity_kwh"]:
        raise RequestError(422, "battery minimum_energy_kwh exceeds capacity_kwh")
    if not b["minimum_energy_kwh"] <= b["initial_energy_kwh"] <= b["capacity_kwh"]:
        raise RequestError(422, "battery initial_energy_kwh must be between minimum and capacity")
    return req
