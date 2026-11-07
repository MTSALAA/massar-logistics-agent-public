# Security

This is a student project built for a course, so treat it as a **well-tested prototype, not a hardened production service**. This page says what it protects, how that is checked, what it does not do, and how to report a problem.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's private reporting on this repository (the *Security* tab, then *Report a vulnerability*), or contact the team leader directly. Include what you did, what you expected and what happened. Do not test against systems you do not own.

## What the agent protects

| Area | Measure |
|------|---------|
| **One company never sees another** | Every table carries the company identifier and every query filters on it. Tests run a full scenario for two companies and check every route, every page and the database file itself. |
| **Deleting a company deletes its data** | Deactivation removes every row of the company from every table. A test reads the tables **from the database file** (not from the code's list) and fails if any is missed or if a new table is not covered. |
| **Who may call** | The platform's calls and the operations API need the company's secret key; it is compared in constant time. A wrong key, or another company's key, is refused. |
| **People signing in to the web interface** | Short access codes per company and role, stored only as salted PBKDF2 hashes and shown once; five wrong tries per minute are blocked; sessions are signed cookies (`HttpOnly`, `SameSite=Lax`, `Secure` on HTTPS) that end when the company is deactivated. |
| **Pages** | Everything that comes from orders or the address bar is escaped; values placed in attributes go through a JSON encoder; actions must come from the interface (a header a foreign site cannot send); pages cannot be framed (`X-Frame-Options`, CSP `frame-ancestors`), are not cached, and do not leak their origin. |
| **External scripts** | The two libraries loaded from a public CDN are pinned to an exact version **and** to a cryptographic hash (Subresource Integrity), so a tampered copy does not run. |
| **Abuse of the server** | Request bodies above 1 MB are refused (even without a declared length); a plan is limited to 500 shipments and 30 seconds of solver time; failed sign-in bookkeeping and the address cache are bounded or per company. |
| **No secrets in the code or the history** | Configuration comes from environment variables. The history was scanned for keys, tokens and passwords. |
| **The container** | The server runs as an unprivileged user (it starts as root only to hand the data volume over, then drops root). |
| **Errors** | An internal failure is answered with a generic message; details and keys are never logged. |
| **Dependencies** | Versions are pinned and checked against public vulnerability databases (see below). |

## How it is checked

- `cd agent && pytest` runs the whole suite, including the security tests (`tests/test_security.py`, `tests/test_final.py`).
- The CI runs `bandit` (static analysis of the code) and `pip-audit` (known vulnerabilities in the dependencies) on every push.

## What it does not do (read before deploying)

- **No encryption in transit.** The agent speaks plain HTTP. Put it behind an HTTPS reverse proxy and set `SESSION_COOKIE_SECURE=1`. Without it, codes and cookies travel in clear on the network.
- **The company's secret key is stored in the database file in clear text**, because the agent must send it to the platform. Anyone who can read the data volume can read it. Protect the volume (file permissions, disk encryption, backups).
- **Activation cannot be authenticated.** It is the call that tells the agent a company's key. The platform's contract only provides a fixed signature header, which the agent does not rely on.
- **Network exposure is yours to restrict.** `docker-compose.yml` publishes port 9000 on all interfaces, which is convenient for a demonstration. In a shared network, restrict it (a firewall, or publish on `127.0.0.1` behind a proxy).
- **The API documentation (`/docs`) is public**, like the code.
- **Access codes are handed out by an operator**; there is no self-service reset.
- **Browser pages need a public CDN** for two small libraries and the map tiles. Copying the libraries into the project would remove that dependency.
- **Outside calls:** address text goes to the public OpenStreetMap geocoder when geocoding is on, and map links tapped by a driver open Google or Apple Maps from the driver's own phone. The full list is in `docs/Rapport-execution.md`.

## Test data

All identifiers, keys and access codes shown in the documentation are fictional examples. The demo values used while developing were generated on a local test platform and are not stored in the repository.
