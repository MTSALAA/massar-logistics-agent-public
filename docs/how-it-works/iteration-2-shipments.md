# Shipments (version 0.3.0)

## What it does

The agent receives the company's orders (`commande`) and stock levels (`stock_level`), decides which orders can ship, creates a shipment for each of them, follows the shipment until it is delivered or returned, and tells the core about every change by publishing an `expedition` object (the Billing agent reads it).

## Receiving objects

Two ways in, both ending in `logistics.ingest()`:

1. **Push:** the core calls `POST /exchange/notify` with `{object_type, external_id, version, payload}`.
2. **Pull (fallback):** the agent reads `GET /exchange/agent/stock_level` and `commande` from the core. This happens at start, right after a company activates, and on demand (`POST /tenant/{id}/sync`). It covers anything missed while the agent was down.

`ingest()` keeps the latest version of each object (`objects` table). An older version arriving late is ignored (`{"processed": false, "stale": true}`). Then it applies the rules:

- a `commande` triggers the **shipping decision** for that order;
- a `stock_level` re-checks the orders that are being **held** for stock.

The reply to the core is sent first; publishing and locating addresses happen after the reply (the core waits for the answer, and the agent must not make it wait).

## The shipping decision

Evaluated by `logistics.evaluate_order()` each time an order or a stock level changes. The result is stored per order (`GET /tenant/{id}/orders`).

| Order status (`statut`) | Decision |
|-------------------------|----------|
| `annulee` | `cancelled`. A shipment still in `preparation` goes to `retour` (reason "Order cancelled before pick-up"); one already on the road is left alone. |
| `expediee`, `facturee` | `closed`: another actor already moved the order on. |
| `en_attente` | `waiting`: not confirmed yet, no alert. |
| `confirmee` (or no status) | Checked below. |

For a confirmed order, in this order:

1. **Lines valid?** `lignes` must be a non-empty list of `{sku, quantite > 0}`. Else `blocked`, alert `order_invalid`.
2. **Address known?** (next section). Else `blocked`, alert `address_missing`.
3. **Stock enough for every line?** The available quantity of a `sku` is the sum of `quantite_disponible` over all its `stock_level` objects (one per warehouse is allowed). Not enough, or no information at all, means:
   - `blocked` with alert `stock_insufficient` or `stock_unknown`, or
   - `held` (same alerts) if the company set `wait_for_stock`. A held order is checked again each time a stock level arrives.
4. Otherwise a **shipment is created** in `preparation`, the decision is `shipped`, and earlier alerts for the order are closed.

A blocked order is not re-checked by itself; it is re-checked when a new version of the order arrives. The same order arriving twice never creates two shipments.

The agent does **not reserve stock**: the Stock agent owns it. Two orders competing for the last units can both ship. Whether `stock_level` is already reduced by confirmed orders is an open question for the instructor (`docs/Adresse-livraison-proposition.md`).

## The delivery address

The registry's `commande` example has no address. After agreement with the team, the agent reads, in this order:

1. `adresse_livraison`, then `client.adresse`: a text, or an object with `rue`, `adresse`, `ligne1`, `ligne2`, `code_postal`, `ville`, `wilaya`, `pays`.
2. Coordinates: `latitude`/`longitude` (or `lat`/`lon`) inside the address object, or at the top of the order.

The address is "known" when there is a text or a pair of valid coordinates. Out-of-range or non-numeric coordinates are ignored.

## The shipment and its life cycle

A shipment has an id `EXP-<order>`, a tracking number `MSR-XXXXXXXX` (derived from the company and order, so it never changes), the carrier "Massar Logistique", the address and coordinates.

```
 preparation ──> en_transit ──> livree
      │              │
      └──────────────┴────────> retour   (a reason is required)
```

`livree` and `retour` are final. `logistics.change_status()` refuses any other move (409), unknown statuses (422), a `retour` without a reason (422) and unknown shipments (404). Each valid move writes a timestamped event (ISO 8601 UTC) in `shipment_events`; the shipment's `history` returns them in order.

## Publishing to the core

`core_client.publish_pending()` sends every shipment whose latest state the core does not have yet:

```
POST {CORE_URL}/exchange/agent/expedition
Authorization: Bearer <company key>      X-Tenant-Id: <company>
{ "external_id": "EXP-CMD-1",
  "payload": { "numero_expedition": "EXP-CMD-1", "reference_commande": "CMD-1",
               "transporteur": "Massar Logistique", "numero_suivi": "MSR-1A2B3C4D", "statut": "en_transit" } }
```

The same `external_id` is sent on every change, so the core keeps one object and raises its `version`. If the core is down or refuses, nothing is lost: the shipment stays "pending" (`published.pending: true`) and is sent on the next event or sync. If a shipment changes while a publication is in flight, it stays pending. Without `CORE_URL` the agent works offline.

## The operations API

Provisional sign-in: `X-Tenant-Id` plus the company key as Bearer token (a real sign-in for managers and drivers comes with the interface). All routes are under `/tenant/{tenant_id}`; the path company must match the header.

| Route | Purpose |
|-------|---------|
| `GET /shipments`, `GET /shipments/{id}` | Shipments with history and publication state |
| `POST /shipments/{id}/status` | `{"status": "en_transit" \| "livree" \| "retour", "reason": "..."}` |
| `GET /orders` | The decision taken for each order |
| `GET /alerts` | Open alerts (`?include_resolved=true` for all) |
| `POST /sync` | Pull from the core, then publish what is pending |

## Data (all with `tenant_id`, all in `TENANT_TABLES`)

`objects` (received orders and stock), `order_decisions`, `shipments`, `shipment_events`, `alerts` (one open alert per order and kind).

## Try it

With the agent and the core running (see [Running with the platform core](../Running-with-the-platform-core.md)), once a company has subscribed:

Publish a stock level and then an order from the core's `/docs` (`POST /companies/{company_id}/exchange/stock_level`, then `.../commande`), logged in as that company. A minimal order:

```json
{ "external_id": "CMD-1",
  "payload": { "numero_commande": "CMD-1", "statut": "confirmee",
               "lignes": [ { "sku": "SKU-1", "quantite": 2 } ],
               "adresse_livraison": "12 rue des Freres, Bejaia" } }
```

Then read the shipment on the agent (the key is the company's `tenant_api_key`, stored in the core's `agent_subscriptions` table):

```bash
curl -H "X-Tenant-Id: <company-id>" -H "Authorization: Bearer <key>" http://localhost:9000/tenant/<company-id>/shipments
```

and the published `expedition` in the core's `exchange_objects` table (or through the dashboard's exchange feed).

## Tests

`tests/test_logistics.py` (decision rules, address formats, life cycle, isolation, purge), `tests/test_core_client.py` (publishing and pulling against a simulated core, retries, no key in logs), `tests/test_ops_api.py` (the routes, keys, tenants).

## Limits

- No stock reservation (see above).
- A blocked order waits for a new version of the order; it is not retried automatically.
- Objects published through the core itself (rather than posted to the agent) still need a manual test with a company login.
