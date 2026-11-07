"""Web interface: sign-in, manager dashboard, route map, driver page and shipment pages.

Server-rendered pages (Jinja2) made lively with HTMX; the maps use Leaflet. These pages are for people
using a browser, under /ui. They are not part of the integration contract (the core never calls them), so
they answer HTML instead of the JSON envelope. Every page except the sign-in needs a valid session, and
every action (POST) must come from HTMX (the `HX-Request` header), which a page of another site cannot
send: that is the protection against forged requests, together with the SameSite cookie.

Managers see everything and can plan. Drivers see their delivery list, the map and the shipments, and report
progress. Both can correct the location of a shipment that is still in preparation.
"""
import os
from datetime import date, timedelta
from html import escape
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import core_client, db, indicators, logistics, planning, ui_auth
from .version import VERSION

STATIC = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter(prefix="/ui", include_in_schema=False)

DRIVER_ACTIONS = {"en_transit", "livree"}  # a return goes through the failure form
LABELS = {"preparation": "To prepare", "en_transit": "On the road", "livree": "Delivered", "retour": "Returned"}
COLORS = ["#1f77b4", "#e8742a", "#2e7d32", "#8e24aa", "#c62828", "#00838f", "#6d4c41", "#455a64"]
STATUS_ORDER = {"en_transit": 0, "preparation": 1, "retour": 2, "livree": 3}
TABLE_LIMIT = 150

# Alerts in words: a title and what to do about it.
ALERT_HELP = {
    "address_missing": ("Address missing", "Add the delivery address to the order and send it again."),
    "address_not_found": ("Address not found", "Correct the address in the order, or set the location by hand on the shipment page."),
    "address_not_located": ("Needs coordinates", "Send latitude and longitude with the order, or set the location by hand on the shipment page."),
    "stock_insufficient": ("Not enough stock", "Wait for the stock to be refilled, then send the order again (or let the company hold orders for stock)."),
    "stock_unknown": ("Stock unknown", "No stock level exists for this product yet: publish it from the stock dashboard."),
    "order_invalid": ("Order not usable", "The order needs at least one line with a product code and a quantity above 0."),
    "unplanned": ("Not planned", "Add vehicle capacity or working hours, or deliver it another day."),
    "location_suspect": ("Suspicious location", "Check the address, or set the location by hand on the shipment page."),
    "delay": ("Running late", "Contact the driver: the delivery is past its planned time."),
    "delivery_failed": ("Delivery failed", "Reschedule the delivery or contact the customer."),
}


MAX_NAV_STOPS = 10  # Google Maps accepts a destination and up to nine stops in between


def nav_url(lat: float, lon: float) -> str:
    """Opens the phone's map app with turn-by-turn directions to the point, from where the driver is."""
    return f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=driving"


def apple_url(lat: float, lon: float) -> str:
    return f"https://maps.apple.com/?daddr={lat:.6f},{lon:.6f}&dirflg=d"


def route_nav_url(points: list[tuple[float, float]]) -> str | None:
    """One link for the next stops in visiting order (the last one is the destination, the others are stops)."""
    points = points[:MAX_NAV_STOPS]
    if not points:
        return None
    lat, lon = points[-1]
    url = f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=driving"
    if len(points) > 1:
        url += "&waypoints=" + "%7C".join(f"{a:.6f},{b:.6f}" for a, b in points[:-1])
    return url


templates.env.globals.update(nav_url=nav_url, apple_url=apple_url)


class LoginRequired(Exception):
    """Raised when a page needs a session the visitor does not have."""


def session(request: Request, roles: tuple[str, ...] = ui_auth.ROLES) -> dict:
    found = ui_auth.read_session(request.cookies.get(ui_auth.COOKIE))
    if found is None:
        raise LoginRequired()
    if found["role"] not in roles:
        raise StarletteHTTPException(403, "This page is for another role")
    return found


def require_htmx(request: Request) -> None:
    if request.headers.get("hx-request") != "true":
        raise StarletteHTTPException(403, "Actions must come from the interface")


async def form(request: Request) -> dict[str, str]:
    """Read an application/x-www-form-urlencoded body (what HTMX and plain forms send)."""
    raw = (await request.body()).decode("utf-8", "replace")
    return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}


def asset_version() -> str:
    """Changes whenever the release or the stylesheet changes, so browsers never keep an outdated copy."""
    try:
        return f"{VERSION}-{int((STATIC / 'app.css').stat().st_mtime)}"
    except OSError:
        return VERSION


def page(request: Request, name: str, who: dict | None, status_code: int = 200, active: str = "", **context) -> HTMLResponse:
    base = {
        "who": who, "active": active, "labels": LABELS, "colors": COLORS, "v": asset_version(),
        "today": date.today().isoformat(), "tomorrow": (date.today() + timedelta(days=1)).isoformat(),
    }
    return templates.TemplateResponse(request, name, {**base, **context}, status_code=status_code)


def problem_box(message: str) -> HTMLResponse:
    return HTMLResponse(f'<div class="error" role="alert">{escape(message)}</div>')


def error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    """What a person sees instead of a bare JSON error when something under /ui goes wrong."""
    if request.headers.get("hx-request") == "true":
        return HTMLResponse(f'<div class="error" role="alert">{escape(message)}</div>', status_code=status_code)
    who = ui_auth.read_session(request.cookies.get(ui_auth.COOKIE))
    titles = {403: "Not allowed", 404: "Not found"}
    return page(request, "message.html", who, status_code, title=titles.get(status_code, "Something went wrong"), text=message)


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, tenant: str = ""):
    return page(request, "login.html", None, tenant=tenant, error=None)


@router.post("/login")
async def login(request: Request):
    data = await form(request)
    tenant_id, code = data.get("tenant", "").strip()[:100], data.get("code", "")[:40]  # real values are shorter
    key = f"{tenant_id}|{request.client.host if request.client else '?'}"
    if ui_auth.throttled(key):
        return page(request, "login.html", None, 429, tenant=tenant_id, error="Too many attempts. Wait a minute.")
    role = ui_auth.check_code(tenant_id, code) if tenant_id and db.get_tenant(tenant_id) else None
    if role is None:
        ui_auth.record_failure(key)
        return page(
            request, "login.html", None, 401, tenant=tenant_id,
            error="Unknown company or wrong code. The company is identified by its long identifier, not by its name.",
        )
    ui_auth.clear_failures(key)
    response = RedirectResponse("/ui" if role == "manager" else "/ui/driver", status_code=303)
    response.set_cookie(
        ui_auth.COOKIE, ui_auth.make_session(tenant_id, role), max_age=ui_auth.SESSION_SECONDS,
        httponly=True, samesite="lax", path="/ui",
        # Behind HTTPS the cookie must never travel in clear: automatic on https, or set SESSION_COOKIE_SECURE=1
        # when a proxy terminates TLS in front of the agent.
        secure=request.url.scheme == "https" or os.getenv("SESSION_COOKIE_SECURE") == "1",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(ui_auth.COOKIE, path="/ui")
    return response


# ---------------------------------------------------------------------------
# Manager dashboard
# ---------------------------------------------------------------------------
def shipment_rows(tenant_id: str) -> list[dict]:
    """Shipments with their place in the latest plan (vehicle, order of visit, planned arrival)."""
    plan = db.latest_plan(tenant_id)
    stops = {s["shipment_id"]: s for s in (plan["stops"] if plan else [])}
    suspect = {w["shipment_id"] for w in (plan.get("warnings", []) if plan else [])}  # far from any road
    rows = []
    for s in db.list_shipments(tenant_id):
        stop = stops.get(s["shipment_id"]) if s["status"] in ("preparation", "en_transit") else None
        rows.append({**s, "vehicle": stop["vehicle_id"] if stop else None, "seq": stop["seq"] if stop else None,
                     "eta": stop["eta"] if stop else None, "suspect": bool(stop) and s["shipment_id"] in suspect})
    return rows


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request):
    who = session(request, ("manager",))
    return page(request, "dashboard.html", who, active="dashboard", tenant=who["tenant"])


@router.get("/fragments/indicators", response_class=HTMLResponse)
def fragment_indicators(request: Request):
    who = session(request, ("manager",))
    indicators.check_delays(who["tenant"])
    return page(request, "_indicators.html", who, ind=indicators.compute(who["tenant"]))


@router.get("/fragments/shipments", response_class=HTMLResponse)
def fragment_shipments(request: Request, status: str = "", q: str = ""):
    who = session(request, ("manager",))
    rows = shipment_rows(who["tenant"])
    needle = q.strip().lower()
    if status in LABELS:
        rows = [r for r in rows if r["status"] == status]
    if needle:
        rows = [r for r in rows if needle in f"{r['shipment_id']} {r['order_ref']} {r['address'] or ''}".lower()]
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), r["vehicle"] or "~", r["seq"] or 0, r["shipment_id"]))
    return page(request, "_shipments.html", who, rows=rows[:TABLE_LIMIT], total=len(rows),
                filtered=bool(status in LABELS or needle))


@router.get("/fragments/alerts", response_class=HTMLResponse)
def fragment_alerts(request: Request):
    who = session(request, ("manager",))
    alerts = []
    for a in db.list_alerts(who["tenant"]):
        title, advice = ALERT_HELP.get(a["kind"], (a["kind"], ""))
        shipment = db.get_shipment_by_order(who["tenant"], a["order_ref"])
        alerts.append({**a, "title": title, "advice": advice, "shipment_id": shipment["shipment_id"] if shipment else None})
    return page(request, "_alerts.html", who, alerts=alerts)


@router.get("/fragments/plan", response_class=HTMLResponse)
def fragment_plan(request: Request):
    who = session(request, ("manager",))
    return page(request, "_plan_summary.html", who, plan=db.latest_plan(who["tenant"]))


@router.post("/plan", response_class=HTMLResponse)
async def plan_now(request: Request):
    who = session(request, ("manager",))
    require_htmx(request)
    data = await form(request)
    result, error = None, None
    try:
        result = planning.plan_routes(who["tenant"], data.get("plan_date") or None, 3)
    except planning.PlanError as exc:
        error = str(exc)
    response = page(request, "_plan_result.html", who, plan=result, error=error)
    response.headers["HX-Trigger"] = "refresh"
    return response


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
@router.get("/map", response_class=HTMLResponse)
def route_map(request: Request, plan: str | None = None):
    who = session(request)
    tenant_id = who["tenant"]
    chosen = db.get_plan(tenant_id, plan) if plan else db.latest_plan(tenant_id)
    depot = (db.get_config(tenant_id) or {}).get("depot")
    data = {"depot": depot, "plan_id": chosen["plan_id"] if chosen else None, "stops": chosen["stops"] if chosen else [],
            "routes": chosen["routes"] if chosen else [], "warnings": chosen.get("warnings", []) if chosen else [],
            "paths": db.get_plan_paths(tenant_id, chosen["plan_id"]) if chosen else {}}
    return page(request, "map.html", who, active="map", plan=chosen, data=data, plans=db.list_plans(tenant_id))


# ---------------------------------------------------------------------------
# Shipment page
# ---------------------------------------------------------------------------
def detail_context(tenant_id: str, shipment_id: str, error: str | None = None, notice: str | None = None) -> dict:
    shipment = db.get_shipment(tenant_id, shipment_id)
    if shipment is None:
        raise StarletteHTTPException(404, "Shipment not found")
    plan = db.latest_plan(tenant_id)
    stop = next((s for s in (plan["stops"] if plan else []) if s["shipment_id"] == shipment_id), None)
    order = (db.get_object(tenant_id, "commande", shipment["order_ref"]) or {}).get("payload", {})
    client = order.get("client") if isinstance(order.get("client"), dict) else {}
    siblings = [s for s in db.list_shipments(tenant_id) if s["order_ref"] == shipment["order_ref"]]
    return {"s": shipment, "events": db.list_events(tenant_id, shipment_id),
            "stop": stop if shipment["status"] in ("preparation", "en_transit") else None,
            "order_client": client.get("nom"), "siblings": siblings, "error": error, "notice": notice}


@router.get("/shipments/{shipment_id}", response_class=HTMLResponse)
def shipment_page(request: Request, shipment_id: str):
    who = session(request)
    return page(request, "shipment.html", who, active="dashboard" if who["role"] == "manager" else "driver",
                **detail_context(who["tenant"], shipment_id))


@router.get("/fragments/shipments/{shipment_id}", response_class=HTMLResponse)
def fragment_shipment(request: Request, shipment_id: str):
    who = session(request)
    return page(request, "_shipment_detail.html", who, **detail_context(who["tenant"], shipment_id))


@router.post("/shipments/{shipment_id}/location", response_class=HTMLResponse)
async def set_location(request: Request, shipment_id: str):
    who = session(request)
    require_htmx(request)
    data = await form(request)
    shipment = db.get_shipment(who["tenant"], shipment_id)
    if shipment is None:
        return problem_box("Shipment not found.")
    if shipment["status"] != "preparation":
        return problem_box("The location can only be changed while the shipment is still to be prepared.")
    try:
        lat, lon = float(data.get("lat", "").replace(",", ".")), float(data.get("lon", "").replace(",", "."))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except ValueError:
        return problem_box("Latitude must be between -90 and 90 and longitude between -180 and 180 (for example 36.7509 and 5.0567).")
    db.set_shipment_location(who["tenant"], shipment_id, lat, lon)
    db.resolve_alerts(who["tenant"], shipment["order_ref"])
    return Response(status_code=200, headers={"HX-Refresh": "true"})


# ---------------------------------------------------------------------------
# Driver page
# ---------------------------------------------------------------------------
def driver_items(tenant_id: str, vehicle: str | None = None) -> list[dict]:
    """Shipments still to do, in the order of the latest plan (unplanned ones last)."""
    items = [r for r in shipment_rows(tenant_id) if r["status"] in ("preparation", "en_transit")]
    if vehicle:
        items = [r for r in items if r["vehicle"] == vehicle]
    return sorted(items, key=lambda r: (r["vehicle"] is None, r["vehicle"] or "", r["seq"] or 0, r["shipment_id"]))


def driver_context(tenant_id: str, vehicle: str | None, error: str | None = None, notice: str | None = None) -> dict:
    plan = db.latest_plan(tenant_id)
    vehicles = sorted({s["vehicle_id"] for s in plan["stops"]}) if plan else []
    items = driver_items(tenant_id, vehicle)
    located = [i for i in items if i["lat"] is not None and not i["suspect"]]  # never send a driver to a doubtful point
    one_route = len({i["vehicle"] for i in located}) <= 1  # one link can only follow one van
    return {"items": items, "vehicles": vehicles, "vehicle": vehicle or "", "error": error, "notice": notice,
            "on_road": sum(1 for i in items if i["status"] == "en_transit"),
            "route_link": route_nav_url([(i["lat"], i["lon"]) for i in located]) if one_route else None,
            "route_link_count": min(len(located), MAX_NAV_STOPS)}


@router.get("/driver", response_class=HTMLResponse)
def driver(request: Request, vehicle: str | None = None):
    who = session(request)
    return page(request, "driver.html", who, active="driver", **driver_context(who["tenant"], vehicle))


@router.get("/fragments/driver", response_class=HTMLResponse)
def fragment_driver(request: Request, vehicle: str | None = None):
    who = session(request)
    return page(request, "_driver_list.html", who, **driver_context(who["tenant"], vehicle))


def after_action(request: Request, who: dict, data: dict, shipment_id: str, error: str | None, notice: str | None) -> HTMLResponse:
    """Answer an action with the part of the page it came from (the driver list or the shipment page)."""
    if data.get("view") == "detail":
        return page(request, "_shipment_detail.html", who, **detail_context(who["tenant"], shipment_id, error, notice))
    return page(request, "_driver_list.html", who, **driver_context(who["tenant"], data.get("vehicle") or None, error, notice))


@router.post("/shipments/{shipment_id}/status", response_class=HTMLResponse)
async def driver_status(request: Request, shipment_id: str, background: BackgroundTasks):
    who = session(request)
    require_htmx(request)
    data = await form(request)
    error = notice = None
    try:
        if data.get("status") not in DRIVER_ACTIONS:
            raise logistics.TransitionError("Use the failure form to report a problem", 422)
        logistics.change_status(who["tenant"], shipment_id, data["status"])
        notice = f"{shipment_id}: {LABELS[data['status']].lower()}"
        background.add_task(core_client.after_change, who["tenant"])
    except logistics.TransitionError as exc:
        error = str(exc)
    return after_action(request, who, data, shipment_id, error, notice)


@router.post("/shipments/{shipment_id}/fail", response_class=HTMLResponse)
async def driver_fail(request: Request, shipment_id: str, background: BackgroundTasks):
    who = session(request)
    require_htmx(request)
    data = await form(request)
    error = notice = None
    try:
        result = logistics.fail_delivery(who["tenant"], shipment_id, data.get("reason"), data.get("reschedule_date") or None)
        again = result["rescheduled"]
        notice = f"{shipment_id}: returned" + (f", rescheduled as {again['shipment_id']} for {again['earliest_date']}" if again else "")
        background.add_task(core_client.after_change, who["tenant"])
    except logistics.TransitionError as exc:
        error = str(exc)
    return after_action(request, who, data, shipment_id, error, notice)
