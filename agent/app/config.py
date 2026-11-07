"""Company configuration received at activation (POST /tenant/activate, body {"config": {...}}).

One model is the single source of truth: it validates the input, fills the defaults and
produces the JSON Schema published on GET /config-schema.

The core's web page subscribes WITHOUT any config, so an empty config must always be valid:
every field has a default. Unknown fields are ignored (data contract, common rules).
"""
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

Capacity = Annotated[float, Field(gt=0, strict=True, description="Maximum load (units)")]


class Depot(BaseModel):
    """Location where every route starts and ends."""

    model_config = ConfigDict(extra="ignore")

    lat: Annotated[float, Field(ge=-90, le=90, strict=True)]
    lon: Annotated[float, Field(ge=-180, le=180, strict=True)]


class Vehicle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # An identifier shown on pages and in links: letters (any language), digits, space, dot, dash, underscore, slash.
    id: Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[\w .\-/]+$")]
    capacity: Capacity


class WorkingHours(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: str = Field("08:00", description="HH:MM")
    end: str = Field("17:00", description="HH:MM")

    @field_validator("start", "end")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not TIME_PATTERN.match(value):
            raise ValueError("must be a time written HH:MM (00:00 to 23:59)")
        return value

    @model_validator(mode="after")
    def _start_before_end(self):
        if self.start >= self.end:  # zero-padded HH:MM compares correctly as text
            raise ValueError("start must be earlier than end")
        return self


def _default_vehicles() -> list[Vehicle]:
    return [Vehicle(id="V1", capacity=1000)]


class TenantConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", title="Massar Logistics agent configuration")

    depot: Depot | None = Field(
        None,
        description="Depot where every route starts and ends. Not needed to receive orders and track "
        "shipments, but required before routes can be optimized.",
    )
    vehicles: list[Vehicle] = Field(
        default_factory=_default_vehicles,
        min_length=1,
        max_length=100,
        description="Available vehicles and their capacities",
    )
    working_hours: WorkingHours = Field(default_factory=WorkingHours)
    stop_duration_minutes: Annotated[float, Field(ge=0, le=480, strict=True)] = Field(
        10, description="Time spent at each delivery stop, in minutes"
    )
    utc_offset_minutes: Annotated[int, Field(ge=-720, le=840, strict=True)] = Field(
        60,
        description="Difference between the company's local time and UTC, in minutes (60 = Algeria). "
        "Working hours and planned arrival times are local; delivery times are recorded in UTC.",
    )
    wait_for_stock: Annotated[bool, Field(strict=True)] = Field(
        False,
        description="If true, an order is held until its stock is available instead of being rejected",
    )

    @field_validator("vehicles")
    @classmethod
    def _unique_vehicle_ids(cls, vehicles: list[Vehicle]) -> list[Vehicle]:
        ids = [v.id for v in vehicles]
        if len(ids) != len(set(ids)):
            raise ValueError("vehicle ids must be unique")
        return vehicles


def parse_config(raw: dict) -> dict:
    """Validate a raw config and return it with every default filled in.

    Raises ValueError with a readable message when something is invalid.
    """
    try:
        return TenantConfig.model_validate(raw).model_dump()
    except ValidationError as exc:
        problems = []
        for e in exc.errors():
            where = ".".join(str(part) for part in e["loc"]) or "config"
            problems.append(f"{where}: {e['msg']}")
        raise ValueError("Invalid config - " + "; ".join(problems)) from None


def config_json_schema() -> dict:
    return TenantConfig.model_json_schema()
