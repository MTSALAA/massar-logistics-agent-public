"""Standard response envelope required by the integration contract (section 4).

Every HTTP response of the agent, on every endpoint and for every error, must be:
    { "status": "ok" | "error", "data": {...} | null, "error": "message" | null }
A response that does not follow it suspends the company's subscription.
"""
from typing import Any


def ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data, "error": None}


def error(message: str) -> dict:
    return {"status": "error", "data": None, "error": message}
