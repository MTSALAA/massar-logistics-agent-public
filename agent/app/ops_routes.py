"""Operations API: follow shipments, change their status, read alerts, force a synchronisation.

Access control: like every call from the core, these routes need X-Tenant-Id and the company's API
key as a Bearer token. They are meant for the platform operator and for scripts; people use the web
interface (ui_routes.py), which has its own access codes. Same response envelope as the rest of the agent.
"""
from fastapi import APIRouter, BackgroundTasks, Header
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import core_client, db, indicators, logistics, planning, ui_auth
from .auth import authenticate
from .envelope import ok

router = APIRouter(prefix="/tenant/{tenant_id}")


def company(tenant_id: str, authorization: str | None, x_tenant_id: str | None) -> str:
    tenant = authenticate(authorization, x_tenant_id)
    if tenant["tenant_id"] != tenant_id:
        raise StarletteHTTPException(403, "Tenant mismatch")
    return tenant_id


def shipment_view(shipment: dict, with_history: bool = True) -> dict:
    view = {
        "shipment_id": shipment["shipment_id"],
        "order_ref": shipment["order_ref"],
        "status": shipment["status"],
        "carrier": shipment["carrier"],
        "tracking_number": shipment["tracking_number"],
        "address": shipment["address"],
        "lat": shipment["lat"],
        "lon": shipment["lon"],
        "failure_reason": shipment["failure_reason"],
        "created_at": shipment["created_at"],
        "updated_at": shipment["updated_at"],
        "published": {"pending": bool(shipment["publish_pending"]), "version": shipment["published_version"]},
    }
    if with_history:
        view["history"] = db.list_events(shipment["tenant_id"], shipment["shipment_id"])
    return view


class StatusBody(BaseModel):
    status: str
    reason: str | None = None
    reschedule_date: str | None = None  # with status "retour": create a new shipment for this ISO date


@router.get("/shipments")
def list_shipments(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    return ok([shipment_view(s) for s in db.list_shipments(tenant_id)])


@router.get("/shipments/{shipment_id}")
def get_shipment(
    tenant_id: str,
    shipment_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    shipment = db.get_shipment(tenant_id, shipment_id)
    if shipment is None:
        raise StarletteHTTPException(404, "Shipment not found")
    return ok(shipment_view(shipment))


@router.post("/shipments/{shipment_id}/status")
def change_shipment_status(
    tenant_id: str,
    shipment_id: str,
    body: StatusBody,
    background: BackgroundTasks,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    try:
        if body.status == "retour":
            result = logistics.fail_delivery(tenant_id, shipment_id, body.reason, body.reschedule_date)
            shipment, again = result["shipment"], result["rescheduled"]
        else:
            shipment, again = logistics.change_status(tenant_id, shipment_id, body.status, body.reason), None
    except logistics.TransitionError as exc:
        raise StarletteHTTPException(exc.code, str(exc)) from None
    background.add_task(core_client.after_change, tenant_id)
    view = shipment_view(shipment)
    if again:
        view["rescheduled_as"] = shipment_view(again, with_history=False)
    return ok(view)


class LocationBody(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class PlanBody(BaseModel):
    plan_date: str | None = None  # ISO date, default today (UTC)
    solver_seconds: float | None = None  # time given to the optimiser, default 5, at most 30


@router.put("/shipments/{shipment_id}/location")
def set_location(
    tenant_id: str,
    shipment_id: str,
    body: LocationBody,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """Set the coordinates by hand (fallback when the address cannot be geocoded)."""
    company(tenant_id, authorization, x_tenant_id)
    shipment = db.get_shipment(tenant_id, shipment_id)
    if shipment is None:
        raise StarletteHTTPException(404, "Shipment not found")
    if shipment["status"] != "preparation":
        raise StarletteHTTPException(409, "The location can only be changed while the shipment is in preparation")
    db.set_shipment_location(tenant_id, shipment_id, body.lat, body.lon)
    db.resolve_alerts(tenant_id, shipment["order_ref"])
    return ok(shipment_view(db.get_shipment(tenant_id, shipment_id), with_history=False))


@router.post("/plans")
def create_plan(
    tenant_id: str,
    body: PlanBody | None = None,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """Plan optimized routes for every shipment in preparation (takes about `solver_seconds`)."""
    company(tenant_id, authorization, x_tenant_id)
    body = body or PlanBody()
    try:
        return ok(planning.plan_routes(tenant_id, body.plan_date, body.solver_seconds))
    except planning.PlanError as exc:
        raise StarletteHTTPException(exc.code, str(exc)) from None


@router.get("/plans")
def list_plans(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    return ok(db.list_plans(tenant_id))


@router.get("/plans/latest")
def latest_plan(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    plan = db.latest_plan(tenant_id)
    if plan is None:
        raise StarletteHTTPException(404, "No plan yet")
    return ok(plan)


@router.get("/plans/{plan_id}/paths")
def get_plan_paths(
    tenant_id: str,
    plan_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """The road line of each vehicle, [[lat, lon], ...] (empty when the plan was made without the road network)."""
    company(tenant_id, authorization, x_tenant_id)
    if db.get_plan(tenant_id, plan_id) is None:
        raise StarletteHTTPException(404, "Plan not found")
    return ok(db.get_plan_paths(tenant_id, plan_id))


@router.get("/plans/{plan_id}")
def get_plan(
    tenant_id: str,
    plan_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    plan = db.get_plan(tenant_id, plan_id)
    if plan is None:
        raise StarletteHTTPException(404, "Plan not found")
    return ok(plan)


@router.get("/orders")
def list_orders(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """The decision taken for each order (shipped, held, blocked, waiting, cancelled, closed)."""
    company(tenant_id, authorization, x_tenant_id)
    return ok(db.list_decisions(tenant_id))


@router.get("/alerts")
def list_alerts(
    tenant_id: str,
    include_resolved: bool = False,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    company(tenant_id, authorization, x_tenant_id)
    return ok(db.list_alerts(tenant_id, open_only=not include_resolved))


@router.get("/indicators")
def get_indicators(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """On-time rate, kilometres per route, distance saved, failures, open alerts."""
    company(tenant_id, authorization, x_tenant_id)
    indicators.check_delays(tenant_id)
    return ok(indicators.compute(tenant_id))


@router.post("/access-codes")
def new_access_codes(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """Create the manager and driver codes of the web interface (replaces the old ones, shown only now)."""
    company(tenant_id, authorization, x_tenant_id)
    return ok({"company": tenant_id, "sign_in": f"/ui/login?tenant={tenant_id}", "codes": ui_auth.generate_codes(tenant_id)})


@router.post("/sync")
def synchronise(
    tenant_id: str,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    """Pull commande and stock_level from the core, then publish what is pending."""
    company(tenant_id, authorization, x_tenant_id)
    return ok(core_client.sync(tenant_id))
