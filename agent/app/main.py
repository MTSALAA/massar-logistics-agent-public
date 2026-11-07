"""Massar Logistics / Delivery agent.

Implements the integration contract v1.0: the 5 mandatory endpoints, /exchange/notify (we consume
objects) and the standard response envelope. On top of it: the shipping decision and shipment life
cycle (logistics.py), the calls to the core (core_client.py) and the operations API (ops_routes.py).
Geocoding and route optimization come in iteration 3, the interface in iteration 4.
"""
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import core_client, db, logistics, maintenance, ops_routes, planning, ui_routes
from .auth import authenticate, check_api_key, require_tenant
from .config import config_json_schema, parse_config
from .envelope import error, ok
from .version import VERSION

CONSUMES = ("commande", "stock_level")  # must match "consumes" in agent.manifest.json

MAX_BODY_BYTES = 1_000_000  # the biggest legitimate request (an order with many lines) is a few kilobytes


class SecurityMiddleware:
    """Pure ASGI middleware: refuses oversized bodies and adds protective headers to the web pages.

    - A body above MAX_BODY_BYTES is refused with 413 (by the Content-Length announced, and by counting
      the bytes that actually arrive, for senders that do not announce one).
    - Pages under /ui are not allowed to be shown inside another site's frame (clickjacking), are not
      cached (a shared computer must not show the previous person's page on "Back"), and do not leak the
      address they came from.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        announced = headers.get(b"content-length", b"0")
        if announced.isdigit() and int(announced) > MAX_BODY_BYTES:
            await self._too_large(send)
            return

        received = 0
        refused = False

        async def counting_receive():
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    refused = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        path = scope.get("path", "")
        is_page = path.startswith("/ui") and not path.startswith("/ui/static")

        async def guarded_send(message):
            if refused and message["type"] == "http.response.start":
                await self._too_large(send)
                return
            if refused:
                return
            if message["type"] == "http.response.start" and path.startswith("/ui"):
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                    (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
                ]
                if is_page:
                    extra += [(b"x-frame-options", b"DENY"), (b"cache-control", b"no-store")]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, counting_receive, guarded_send)

    @staticmethod
    async def _too_large(send):
        body = json.dumps(error("Request body too large")).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pull fallback at start: read what we may have missed while we were down (never blocks the start)."""
    if core_client.core_url() is not None:

        def catch_up():
            for tenant_id in db.list_tenant_ids():
                core_client.sync(tenant_id)

        threading.Thread(target=catch_up, daemon=True).start()
    stop = threading.Event()
    maintenance.start(stop)  # delay alerts and retries of what the core has not received, every few minutes
    yield
    stop.set()


app = FastAPI(title="Massar Logistics / Delivery Agent", version=VERSION, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.include_router(ops_routes.router)
app.include_router(ui_routes.router)
app.mount("/ui/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="ui-static")
started_at = time.time()


# ---------------------------------------------------------------------------
# Every error, whatever its origin, leaves the agent inside the standard envelope.
# ---------------------------------------------------------------------------
@app.exception_handler(ui_routes.LoginRequired)
async def login_required_handler(request: Request, exc: ui_routes.LoginRequired):
    """A page of the interface was asked without a session: send the visitor to the sign-in page."""
    if request.headers.get("hx-request") == "true":  # an HTMX fragment: have the browser leave the page
        return Response(status_code=401, headers={"HX-Redirect": "/ui/login"})
    return RedirectResponse("/ui/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/ui"):  # a person in a browser: a readable page, not JSON
        return ui_routes.error_page(request, exc.status_code, str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content=error(str(exc.detail)))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content=error("Invalid request: " + str(exc.errors())))


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content=error("Internal error"))


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/ui/static/favicon.svg", status_code=301)


class ActivateBody(BaseModel):
    config: dict = Field(default_factory=dict)


class NotifyBody(BaseModel):
    # Unknown fields are ignored silently (data contract, common rules).
    object_type: str
    external_id: str
    version: int = 1
    payload: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# The 5 mandatory endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return ok({"uptime_seconds": int(time.time() - started_at), "version": VERSION})


@app.post("/tenant/activate")
def activate(
    body: ActivateBody,
    background: BackgroundTasks,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    # Activation is the one call that cannot be checked: it is how we learn the key.
    # A new activation of a known tenant replaces its key (the core issues a new one per subscription).
    tenant_id, api_key = require_tenant(authorization, x_tenant_id)
    try:
        config = parse_config(body.config)  # defaults filled in; an empty config is valid
    except ValueError as exc:
        raise StarletteHTTPException(422, str(exc)) from None
    db.upsert_tenant(tenant_id, api_key, config)  # nothing is stored when the config is invalid
    background.add_task(core_client.sync, tenant_id)  # after the reply: take the objects already on the core
    return ok({"activated": True})


@app.post("/tenant/deactivate")
def deactivate(
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    tenant_id, api_key = require_tenant(authorization, x_tenant_id)
    tenant = db.get_tenant(tenant_id)
    if tenant is not None:  # an unknown tenant has nothing to purge: stays idempotent
        check_api_key(tenant, api_key)
    db.purge_tenant(tenant_id)
    return ok({"deactivated": True, "data_purged": True})


@app.get("/tenant/{tenant_id}/status")
def tenant_status(tenant_id: str, x_tenant_id: str | None = Header(None, alias="X-Tenant-Id")):
    if x_tenant_id and x_tenant_id != tenant_id:
        raise StarletteHTTPException(403, "Tenant mismatch")
    if db.get_tenant(tenant_id) is None:
        raise StarletteHTTPException(404, "Tenant not activated")
    return ok({"state": "running"})


@app.get("/config-schema")
def config_schema():
    """JSON Schema of what a company can set when it activates the agent."""
    return ok(config_json_schema())


# ---------------------------------------------------------------------------
# Mandatory for us because we consume objects (commande, stock_level)
# ---------------------------------------------------------------------------
@app.post("/exchange/notify")
def exchange_notify(
    body: NotifyBody,
    background: BackgroundTasks,
    authorization: str | None = Header(None),
    x_tenant_id: str | None = Header(None, alias="X-Tenant-Id"),
):
    tenant = authenticate(authorization, x_tenant_id)
    if body.object_type not in CONSUMES:
        return ok({"processed": False, "ignored": True})
    result = logistics.ingest(tenant["tenant_id"], body.object_type, body.external_id, body.version, body.payload)
    # Publishing talks to the core, which is waiting for this reply: do it after answering.
    background.add_task(core_client.after_change, tenant["tenant_id"])
    background.add_task(planning.locate_shipments, tenant["tenant_id"])  # slow (1 request/s): after the reply
    return ok(result)
