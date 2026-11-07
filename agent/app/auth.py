"""Tenant identification and API key check, shared by the contract endpoints and the operations API."""
import hmac

from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db


def require_tenant(authorization: str | None, x_tenant_id: str | None) -> tuple[str, str]:
    """The core always sends X-Tenant-Id and 'Authorization: Bearer <tenant_api_key>'."""
    if not x_tenant_id:
        raise StarletteHTTPException(400, "Missing X-Tenant-Id header")
    if not authorization or not authorization.startswith("Bearer "):
        raise StarletteHTTPException(401, "Missing or invalid Authorization header")
    return x_tenant_id, authorization[len("Bearer "):]


def check_api_key(tenant: dict, api_key: str) -> None:
    """The key must be the one the core gave us at activation (constant-time comparison)."""
    if not hmac.compare_digest(tenant["api_key"].encode(), api_key.encode()):
        raise StarletteHTTPException(401, "Invalid tenant API key")


def authenticate(authorization: str | None, x_tenant_id: str | None) -> dict:
    """Identify the company and check its key. Returns the tenant row."""
    tenant_id, api_key = require_tenant(authorization, x_tenant_id)
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        raise StarletteHTTPException(404, "Tenant not activated")
    check_api_key(tenant, api_key)
    return tenant
