"""Sign-in of the web interface: access codes per company and a signed session cookie.

Why not the company API key? That key belongs to the core; people who use the interface (logistics
managers, drivers) never see it. Instead, whoever holds the key (the platform operator, through the
operations API) generates two access codes for the company, a `manager` code and a `driver` code, and
hands them out. Only salted hashes of the codes are stored; the codes themselves are shown once, when
they are generated, and never logged. Generating again replaces the old codes.

The session is a cookie signed with HMAC-SHA256 (HttpOnly, SameSite=Lax), valid 12 hours. The signing key
comes from SESSION_SECRET; otherwise a random key is created once and kept in the data volume next to the
database (file `session.key`), so people stay signed in when the agent is restarted or updated.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from . import db

ROLES = ("manager", "driver")
COOKIE = "massar_session"
SESSION_SECONDS = 12 * 3600
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0, O, 1 or I: easy to read out loud
PBKDF2_ROUNDS = 120_000
MAX_FAILURES, WINDOW_SECONDS = 5, 60

_random_secret = secrets.token_bytes(32)
_failures: dict[str, list[float]] = {}


def _normalize(code: str) -> str:
    return code.upper().replace("-", "").replace(" ", "")


def _hash(code: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", _normalize(code).encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()


def new_code() -> str:
    """Twelve symbols out of 32 (60 bits), written XXXX-XXXX-XXXX."""
    symbols = [secrets.choice(ALPHABET) for _ in range(12)]
    return "-".join("".join(symbols[i:i + 4]) for i in range(0, 12, 4))


def generate_codes(tenant_id: str) -> dict[str, str]:
    """Create (or replace) the access codes of a company. Returns them in clear text, once."""
    codes = {}
    for role in ROLES:
        code, salt = new_code(), secrets.token_hex(16)
        db.set_access_code(tenant_id, role, salt, _hash(code, salt))
        codes[role] = code
    return codes


def check_code(tenant_id: str, code: str) -> str | None:
    """The role the code opens for this company, or None."""
    found = None
    for row in db.list_access_codes(tenant_id):  # no early exit: same work whatever the answer
        if hmac.compare_digest(_hash(code, row["salt"]), row["code_hash"]) and found is None:
            found = row["role"]
    return found


# ---------------------------------------------------------------------------
# Throttling of wrong codes (per company and client address)
# ---------------------------------------------------------------------------
MAX_TRACKED_KEYS = 5_000  # an attacker sending endless different company names must not grow memory without limit


def _prune() -> None:
    now = time.monotonic()
    for key in [k for k, v in _failures.items() if not v or now - v[-1] >= WINDOW_SECONDS]:
        del _failures[key]
    while len(_failures) >= MAX_TRACKED_KEYS:  # still full: forget the oldest to make room for one more
        del _failures[next(iter(_failures))]


def throttled(key: str) -> bool:
    recent = [t for t in _failures.get(key, []) if time.monotonic() - t < WINDOW_SECONDS]
    _failures[key] = recent
    return len(recent) >= MAX_FAILURES


def record_failure(key: str) -> None:
    if len(_failures) >= MAX_TRACKED_KEYS:
        _prune()
    _failures.setdefault(key, []).append(time.monotonic())


def clear_failures(key: str) -> None:
    _failures.pop(key, None)


# ---------------------------------------------------------------------------
# Signed session cookie
# ---------------------------------------------------------------------------
def _key_file() -> Path:
    return Path(os.path.dirname(os.getenv("DATABASE_PATH", "/data/agent.db")) or ".") / "session.key"


def _secret() -> bytes:
    configured = (os.getenv("SESSION_SECRET") or "").encode()
    if configured:
        return configured
    path = _key_file()
    try:
        if path.exists():
            return path.read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_random_secret)
        os.chmod(path, 0o600)
        return _random_secret
    except OSError:  # a read-only volume: sessions then last until the next restart
        return _random_secret


def _sign(body: str) -> str:
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()


def make_session(tenant_id: str, role: str) -> str:
    payload = {"tenant": tenant_id, "role": role, "exp": int(time.time()) + SESSION_SECONDS}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{body}.{_sign(body)}"


def read_session(token: str | None) -> dict | None:
    """The session if the signature is right and it has not expired, and the company still exists."""
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(body)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    if payload.get("exp", 0) < time.time() or payload.get("role") not in ROLES:
        return None
    if db.get_tenant(payload.get("tenant", "")) is None:  # the company was deactivated
        return None
    return payload
