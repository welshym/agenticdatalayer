"""
CDC Assembly Service — Business Context Layer
=============================================
Port: 8011

Stateful CDC assembly engine. Receives raw domain events from SoRs,
applies ontology field mappings to produce domain partials, and writes
assembled canonical customer records to the context cache.

Two distinct flows depending on whether the customer already has a cache record:

  Cold start (no cache record): events are held in _pending until all required
  domains have arrived, then merged and written as a complete record. This is
  the only path that performs a genuine multi-domain join.

  Ongoing updates (cache record exists): each incoming event performs a
  domain-scoped upsert into the existing record's field group. No join is
  performed — the cache always holds the last-known state per domain. CDC has
  no mechanism to detect staleness in a domain it has not received a new event
  for; it only knows a domain is out of date when a new event arrives for it.

A background asyncio task checks for timed-out pending assemblies every
5 seconds. On timeout, partial records are written to the cache with
assembly_state="partial_timed_out" if timeout_action="write_partial".

Routes:
  POST /events       — receive a raw SoR event; assemble domain; join or pend
  GET  /event-log    — recent assembly events (newest first)
  GET  /pending      — current pending (in-flight) assemblies
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import ontology

CACHE_URL     = "http://localhost:8012"
LOG_URL       = "http://localhost:8015"
CATALOGUE_URL = "http://localhost:8014"

_event_log: deque[dict] = deque(maxlen=100)
_log_lock = asyncio.Lock()


@dataclass
class PendingAssembly:
    """Tracks an in-flight multi-domain assembly waiting for all required events."""
    customer_id: str
    correlation_id: str
    timeout_action: str                                              # "write_partial" | "discard" — resolved at creation
    domains: dict[str, dict] = field(default_factory=dict)          # domain name → partial canonical dict
    received_events: list[str] = field(default_factory=list)        # event type names received so far
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timeout_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Keyed by customer_id — only one pending assembly per customer at a time
_pending: dict[str, PendingAssembly] = {}
_pending_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Lifespan — shared HTTP clients + background timeout checker
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cache_http     = httpx.AsyncClient(base_url=CACHE_URL,     timeout=10.0)
    app.state.log_http       = httpx.AsyncClient(base_url=LOG_URL,       timeout=3.0)
    app.state.catalogue_http = httpx.AsyncClient(base_url=CATALOGUE_URL, timeout=5.0)
    task = asyncio.create_task(_timeout_checker(app))
    yield
    task.cancel()
    await app.state.cache_http.aclose()
    await app.state.log_http.aclose()
    await app.state.catalogue_http.aclose()


app = FastAPI(
    title="CDC Assembly",
    description="Change Data Capture & Stateful Assembly Service — Business Context Layer",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def get_cache_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.cache_http


async def get_log_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.log_http


async def get_catalogue_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.catalogue_http


# ---------------------------------------------------------------------------
# Log helper — fire and forget; logging must never break the assembly path
# ---------------------------------------------------------------------------
async def _log(
    log_http: httpx.AsyncClient,
    service: str,
    action: str,
    reason: str,
    customer_id: str | None = None,
    event_id: str | None = None,
    correlation_id: str | None = None,
    outcome: str = "success",
    details: dict | None = None,
) -> None:
    try:
        await log_http.post("/logs", json={
            "service": service,
            "action": action,
            "reason": reason,
            "customer_id": customer_id,
            "event_id": event_id,
            "correlation_id": correlation_id,
            "outcome": outcome,
            "details": details or {},
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background timeout checker — runs every 5 seconds
# ---------------------------------------------------------------------------
async def _timeout_checker(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(5)
        now = datetime.now(timezone.utc)
        timed_out: list[str] = []

        async with _pending_lock:
            for cid, pa in _pending.items():
                if now >= pa.timeout_at:
                    timed_out.append(cid)

        for cid in timed_out:
            async with _pending_lock:
                pa = _pending.pop(cid, None)
            if pa is None:
                continue

            if pa.timeout_action == "write_partial":
                partial = (
                    ontology.merge_domains(pa.domains)
                    if len(pa.domains) > 1
                    else next(iter(pa.domains.values()), {})
                )
                missing = [d for d in ("customer", "billing") if d not in pa.domains]
                partial["assembly_state"] = "partial_timed_out"
                partial["missing_domains"] = missing

                try:
                    await app.state.cache_http.put(f"/records/{cid}", json=partial)
                except Exception:
                    pass

                await _log(
                    app.state.log_http, "cdc-assembly", "timeout_triggered",
                    f"Assembly timed out for {cid} — only domains {list(pa.domains.keys())} received; writing partial",
                    customer_id=cid, correlation_id=pa.correlation_id,
                    outcome="timeout",
                    details={"received_domains": list(pa.domains.keys()), "missing_domains": missing},
                )

            log_entry = {
                "event_id": pa.correlation_id,
                "customer_id": cid,
                "action": "timeout_triggered",
                "correlation_id": pa.correlation_id,
                "received_domains": list(pa.domains.keys()),
                "assembly_state": "partial_timed_out",
            }
            async with _log_lock:
                _event_log.appendleft(log_entry)


# ---------------------------------------------------------------------------
# Enrichment helpers — called after every complete assembly write
# ---------------------------------------------------------------------------

async def _enrich_and_write(
    customer_id: str,
    cache_http: httpx.AsyncClient,
    catalogue_http: httpx.AsyncClient,
    log_http: httpx.AsyncClient,
) -> None:
    """
    Read the assembled cache record, enrich each subscription line with
    product_name, product_type, and list_price_gbp from the Product Catalogue,
    then derive the discount summary arithmetically from list_price_gbp (Catalogue)
    minus monthly_charge (Billing, already in the assembled record). Write the
    updated commercial_state back using a domain-merge upsert.

    Billing stores the actual post-PoS-discount charge in monthly_charge — the
    discount is implicit in the delta against list price. No Offer Engine call is
    needed during assembly; the Offer Engine is an agent advisory tool only.

    Called inline (awaited) after every write that results in assembly_state=complete
    so that the cache always holds fully enriched context before the caller returns.
    Failures are silent — enrichment must never interrupt the assembly path.
    """
    try:
        record_resp = await cache_http.get(f"/records/{customer_id}")
        if record_resp.status_code != 200:
            return
        record = record_resp.json()

        commercial = record.get("commercial_state")
        if not commercial:
            return

        subscriptions = commercial.get("subscriptions", [])
        enriched_subs: list[dict] = []
        for sub in subscriptions:
            sku = sub.get("product_id")
            enriched_sub = dict(sub)
            if sku:
                try:
                    cat_resp = await catalogue_http.get(f"/products/{sku}")
                    if cat_resp.status_code == 200:
                        cat = cat_resp.json()
                        enriched_sub["product_name"]           = cat.get("product_name")
                        enriched_sub["product_type"]           = cat.get("product_type")
                        enriched_sub["list_price_gbp"]         = cat.get("list_price_gbp")
                        enriched_sub["available_terms_months"] = cat.get("available_terms_months")
                except Exception:
                    pass
            enriched_subs.append(enriched_sub)

        # Derive discount from Catalogue list price vs Billing actual charge.
        # monthly_charge is the real charge after any PoS discounts already applied.
        per_subscription: dict[str, dict] = {}
        total_list_gbp   = 0.0
        total_actual_gbp = 0.0
        for sub in enriched_subs:
            list_price = sub.get("list_price_gbp")
            actual     = sub.get("monthly_charge")
            sku        = sub.get("product_id", "unknown")
            if list_price is not None and actual is not None:
                saving = round(list_price - actual, 2)
                per_subscription[sku] = {
                    "list_price_gbp":    list_price,
                    "actual_charge_gbp": actual,
                    "saving_gbp":        saving,
                }
                total_list_gbp   += list_price
                total_actual_gbp += actual

        discount_summary = {
            "per_subscription": per_subscription,
            "total_list_gbp":   round(total_list_gbp, 2),
            "total_actual_gbp": round(total_actual_gbp, 2),
            "total_saving_gbp": round(total_list_gbp - total_actual_gbp, 2),
        }

        now = datetime.now(timezone.utc).isoformat()
        updated_meta = {**commercial.get("_meta", {}), "assembled_at": now}
        updated_commercial = {
            **commercial,
            "subscriptions":    enriched_subs,
            "discount_summary": discount_summary,
            "_meta":            updated_meta,
        }

        await cache_http.put(f"/records/{customer_id}", json={
            "commercial_state": updated_commercial,
            "_merge_domain":    "enrichment",
        })

        await _log(
            log_http, "cdc-assembly", "enrichment_written",
            f"Subscriptions enriched and discounts derived for {customer_id} — "
            f"{len(enriched_subs)} subscription(s), "
            f"total saving £{discount_summary['total_saving_gbp']:.2f}",
            customer_id=customer_id,
            details={
                "subscription_count": len(enriched_subs),
                "total_list_gbp":     discount_summary["total_list_gbp"],
                "total_actual_gbp":   discount_summary["total_actual_gbp"],
                "total_saving_gbp":   discount_summary["total_saving_gbp"],
            },
        )
    except Exception:
        pass


async def _handle_product_update(
    sku: str,
    cache_http: httpx.AsyncClient,
    catalogue_http: httpx.AsyncClient,
    log_http: httpx.AsyncClient,
) -> None:
    """
    Fan-out handler for product.catalogue.updated events. Reads all cache records,
    identifies customers whose commercial_state contains the affected SKU, and
    re-enriches each. Run as a background asyncio task so the Catalogue PATCH
    response is not blocked on a potentially large fan-out.
    """
    try:
        all_resp = await cache_http.get("/records")
        if all_resp.status_code != 200:
            return
        all_records: dict[str, dict] = all_resp.json()
    except Exception:
        return

    affected: list[str] = [
        cid for cid, rec in all_records.items()
        if any(
            sub.get("product_id") == sku
            for sub in rec.get("commercial_state", {}).get("subscriptions", [])
        )
    ]

    await _log(
        log_http, "cdc-assembly", "product_update_fanout",
        f"product.catalogue.updated for {sku} — "
        f"re-enriching {len(affected)} affected customer(s): {affected}",
        details={"sku": sku, "affected_customer_count": len(affected), "affected_customers": affected},
    )

    for cid in affected:
        await _enrich_and_write(cid, cache_http, catalogue_http, log_http)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------
class SorEvent(BaseModel):
    raw_record: dict[str, Any]
    source_system: str
    event_type: str
    event_id: str | None = None


# ---------------------------------------------------------------------------
# POST /events — main assembly logic
# ---------------------------------------------------------------------------
@app.post("/events", summary="Receive a raw SoR event; assemble domain; join or pend")
async def process_event(
    event: SorEvent,
    cache_http:     httpx.AsyncClient = Depends(get_cache_http),
    log_http:       httpx.AsyncClient = Depends(get_log_http),
    catalogue_http: httpx.AsyncClient = Depends(get_catalogue_http),
) -> dict[str, Any]:
    """
    Assembly pipeline:
      1. Resolve event_type — product.catalogue.updated events trigger a background
         fan-out enrichment of all affected customers; return immediately.
      2. For customer events: apply domain field mappings → partial canonical dict
      3. Cache HIT  → domain merge upsert on existing record; if now complete, enrich inline
      4. Cache MISS → check pending store
          a. Pending exists + all domains now present → merge + write to cache; enrich inline
          b. Pending exists but incomplete           → update pending + log join_pending
          c. No pending                              → create pending + log join_pending
    """
    event_id = event.event_id or str(uuid.uuid4())

    if event.event_type == ontology.PRODUCT_EVENT_TYPE:
        canonical = ontology.assemble_product_update(event.raw_record)
        sku = canonical.get("product_id")
        if not sku:
            return {"error": "product event missing sku field", "event_id": event_id}
        asyncio.create_task(
            _handle_product_update(sku, cache_http, catalogue_http, log_http)
        )
        await _log(
            log_http, "cdc-assembly", "product_event_received",
            f"product.catalogue.updated received for {sku} — fan-out enrichment scheduled",
            details={"sku": sku, "event_id": event_id},
        )
        return {
            "event_id":   event_id,
            "event_type": event.event_type,
            "sku":        sku,
            "status":     "fan_out_scheduled",
        }

    spec = ontology.ASSEMBLY_SPEC.get(event.event_type)

    if spec is None:
        return {"error": f"Unknown event_type: {event.event_type}", "event_id": event_id}

    # Step 2: assemble domain partial
    domain_partial = ontology.assemble_domain(event.raw_record, event.event_type, event_id)
    customer_id = domain_partial.get(spec.join_key_canonical)

    if not customer_id:
        return {"error": "Could not extract customer_id from raw record", "event_id": event_id}

    await _log(
        log_http, "cdc-assembly", "event_received",
        f"{event.event_type} received from {event.source_system} for {customer_id}",
        customer_id=customer_id, event_id=event_id,
        details={"event_type": event.event_type, "domain": spec.domain},
    )

    # Step 3: check cache
    cache_resp = await cache_http.get(f"/records/{customer_id}")

    if cache_resp.status_code == 200:
        existing = cache_resp.json()
        existing_state = existing.get("assembly_state", "complete")

        # Determine new state by checking which field groups are present in the
        # existing record. "profile" and "commercial_state" are the field group
        # keys written by assemble_domain() for the customer and product domains
        # respectively; their presence indicates that domain has been written at
        # least once, regardless of how recently.
        has_customer = "profile"          in existing or spec.domain == "customer"
        has_billing  = "commercial_state" in existing or spec.domain == "billing"
        new_state = "complete" if (has_customer and has_billing) else (
            "awaiting_billing" if has_customer else "awaiting_crm"
        )

        # Reuse correlation_id if the existing record carries one
        correlation_id = (
            existing.get("provenance", {}).get("domains", {})
            .get(spec.domain, {}).get("correlation_id")
            or str(uuid.uuid4())
        )

        upsert_body = {**domain_partial, "assembly_state": new_state, "_merge_domain": spec.domain}
        put_resp = await cache_http.put(f"/records/{customer_id}", json=upsert_body)
        put_resp.raise_for_status()
        stored = put_resp.json()

        await _log(
            log_http, "cdc-assembly", "cache_written",
            f"domain upsert — {spec.domain} domain updated for {customer_id}"
            + (" — record now complete" if new_state == "complete" else f" — still {new_state}"),
            customer_id=customer_id, event_id=event_id, correlation_id=correlation_id,
            details={"domain": spec.domain, "assembly_state": new_state, "previous_state": existing_state},
        )

        async with _log_lock:
            _event_log.appendleft({
                "event_id": event_id, "customer_id": customer_id,
                "action": "cache_written", "domain": spec.domain,
                "assembly_state": new_state, "version": stored["version"],
                "correlation_id": correlation_id,
            })

        if new_state == "complete":
            await _enrich_and_write(customer_id, cache_http, catalogue_http, log_http)

        return {
            "event_id": event_id, "customer_id": customer_id,
            "assembly_status": new_state, "correlation_id": correlation_id,
            "domain": spec.domain,
        }

    # Step 4: cache MISS — pending store logic
    do_write = False
    correlation_id = ""
    merged: dict = {}

    async with _pending_lock:
        pa = _pending.get(customer_id)

        if pa is not None:
            pa.domains[spec.domain] = domain_partial
            pa.received_events.append(event.event_type)
            required = {"customer", "billing"}
            correlation_id = pa.correlation_id

            if required <= set(pa.domains.keys()):
                # All domains present — prepare merge outside the lock
                merged = ontology.merge_domains(pa.domains)
                merged["assembly_state"] = "complete"
                merged["missing_domains"] = []
                del _pending[customer_id]
                do_write = True
            else:
                missing = list(required - set(pa.domains.keys()))

        else:
            # First event for this customer — create pending entry.
            # Timeout and action are resolved once across all event types in the
            # join so the policy is not re-derived from whichever event arrived first.
            correlation_id = str(uuid.uuid4())
            all_join_event_types = [event.event_type] + spec.join_with
            join_specs = [
                ontology.ASSEMBLY_SPEC[et]
                for et in all_join_event_types
                if et in ontology.ASSEMBLY_SPEC
            ]
            max_timeout = max(s.timeout_seconds for s in join_specs)
            # Most permissive: write_partial wins over discard so a partial record
            # is always written if any event type in the join declares it.
            resolved_timeout_action = (
                "write_partial"
                if any(s.timeout_action == "write_partial" for s in join_specs)
                else "discard"
            )
            timeout_at = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + max_timeout,
                tz=timezone.utc,
            )
            pa = PendingAssembly(
                customer_id=customer_id,
                correlation_id=correlation_id,
                timeout_action=resolved_timeout_action,
                domains={spec.domain: domain_partial},
                received_events=[event.event_type],
                first_seen=datetime.now(timezone.utc),
                timeout_at=timeout_at,
            )
            _pending[customer_id] = pa
            missing = [d for d in ("customer", "billing") if d != spec.domain]

    if do_write:
        put_resp = await cache_http.put(f"/records/{customer_id}", json=merged)
        put_resp.raise_for_status()
        stored = put_resp.json()

        await _log(
            log_http, "cdc-assembly", "join_completed",
            f"All domains received for {customer_id} — assembled complete canonical record",
            customer_id=customer_id, event_id=event_id, correlation_id=correlation_id,
            details={"domains": ["customer", "billing"], "version": stored["version"]},
        )

        async with _log_lock:
            _event_log.appendleft({
                "event_id": event_id, "customer_id": customer_id,
                "action": "join_completed", "assembly_state": "complete",
                "version": stored["version"], "correlation_id": correlation_id,
            })

        await _enrich_and_write(customer_id, cache_http, catalogue_http, log_http)

        return {
            "event_id": event_id, "customer_id": customer_id,
            "assembly_status": "complete", "correlation_id": correlation_id,
            "domain": spec.domain,
        }

    # Pending — not yet complete
    assembly_state = f"awaiting_{'billing' if spec.domain == 'customer' else 'crm'}"

    await _log(
        log_http, "cdc-assembly", "join_pending",
        f"{spec.domain} domain received for {customer_id} — waiting for {missing}",
        customer_id=customer_id, event_id=event_id, correlation_id=correlation_id,
        outcome="partial",
        details={"domain": spec.domain, "missing_domains": missing, "assembly_state": assembly_state},
    )

    async with _log_lock:
        _event_log.appendleft({
            "event_id": event_id, "customer_id": customer_id,
            "action": "join_pending", "domain": spec.domain,
            "assembly_state": assembly_state, "missing_domains": missing,
            "correlation_id": correlation_id,
        })

    return {
        "event_id": event_id, "customer_id": customer_id,
        "assembly_status": assembly_state, "correlation_id": correlation_id,
        "domain": spec.domain, "missing_domains": missing,
    }


@app.get("/event-log", summary="Recent CDC assembly events (newest first)")
async def event_log(limit: int = 20) -> list[dict]:
    async with _log_lock:
        return list(_event_log)[:limit]


@app.get("/pending", summary="Current pending (in-flight) assemblies")
async def get_pending() -> list[dict]:
    async with _pending_lock:
        return [
            {
                "customer_id":      pa.customer_id,
                "correlation_id":   pa.correlation_id,
                "domains_received": list(pa.domains.keys()),
                "missing_domains":  [d for d in ("customer", "billing") if d not in pa.domains],
                "first_seen":       pa.first_seen.isoformat(),
                "timeout_at":       pa.timeout_at.isoformat(),
                "received_events":  pa.received_events,
            }
            for pa in _pending.values()
        ]


if __name__ == "__main__":
    uvicorn.run("cdc_app:app", host="0.0.0.0", port=8011, reload=True)
