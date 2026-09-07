"""
Real-Time Context Cache Service — Business Context Layer
=========================================================
Port: 8012

Two-tier in-memory store for assembled canonical customer entities.

  Tier 1 (_permanent): All customers ever assembled. Never evicted.
    Equivalent to the Cosmos DB permanent index in the full design —
    the floor that guarantees every known customer is always retrievable.

  Tier 2/3 (_store): The hot cache. Same data as Tier 1 for the customers
    it holds, but evictable. Equivalent to the Redis layer in the full design.
    In this demo there is no eviction policy; the distinction from Tier 1 is
    structural, ready for a TTL/promotion mechanism to be layered on top.

CDC Assembly writes to both tiers on every successful assembly.
The ACG reads _store first; on a miss it reads _permanent before
falling back to triggering on-demand assembly from the SoRs.

Cache records carry an `assembly_state` field:
  complete | awaiting_billing | awaiting_customer | partial_timed_out

Merge upsert: if PUT body contains `_merge_domain`, only the keys
present in the body are updated; existing keys not in the body are
preserved. This lets CDC update a single domain (e.g. just the customer
identity fields) without wiping out the product data already in the record.

Routes:
  PUT  /records/{customer_id}   — upsert to both tiers (full or merge)
  GET  /records/{customer_id}   — read from hot cache (404 on miss)
  GET  /records                 — snapshot of hot cache
  GET  /permanent/{customer_id} — read from permanent store (404 only if unknown)
  GET  /permanent               — snapshot of permanent store
  GET  /status                  — entry counts for both tiers
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

import ontology

LOG_URL = "http://localhost:8015"

_store:     dict[str, dict[str, Any]] = {}  # Tier 2/3 — hot cache
_permanent: dict[str, dict[str, Any]] = {}  # Tier 1   — permanent store
_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.log_http = httpx.AsyncClient(base_url=LOG_URL, timeout=3.0)
    yield
    await app.state.log_http.aclose()


app = FastAPI(
    title="Context Cache",
    description="Real-Time Context Cache — Business Context Layer",
    version="3.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def _log(
    request: Request,
    service: str,
    action: str,
    reason: str,
    customer_id: str | None = None,
    outcome: str = "success",
    details: dict | None = None,
) -> None:
    """Fire-and-forget: log errors here must never block the write path."""
    try:
        await request.app.state.log_http.post("/logs", json={
            "service": service,
            "action": action,
            "reason": reason,
            "customer_id": customer_id,
            "outcome": outcome,
            "details": details or {},
        })
    except Exception:
        pass


@app.put("/records/{customer_id}", summary="Write (upsert) a canonical entity to both cache tiers")
async def write_record(
    customer_id: str,
    body: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    # Pop the merge hint before storing — it's a write instruction, not a data field
    merge_domain = body.pop("_merge_domain", None)

    violations = ontology.validate_cache_record(body)
    if violations:
        raise HTTPException(status_code=422, detail={"violations": violations})

    async with _lock:
        existing = _store.get(customer_id)
        version = (existing["version"] + 1) if existing else 1

        if merge_domain and existing:
            # Merge upsert: update only keys in body, preserve keys not in body.
            # This lets CDC update one domain without losing the other domain's fields.
            stored = {**existing, **body, "version": version}
        else:
            stored = {**body, "version": version}

        _store[customer_id] = stored
        # Permanent store uses the same merge logic but tracks its own version
        # independently so that hot-cache eviction and re-assembly do not reset it.
        existing_perm = _permanent.get(customer_id)
        perm_version = (existing_perm["version"] + 1) if existing_perm else 1
        if merge_domain and existing_perm:
            permanent_stored = {**existing_perm, **body, "version": perm_version}
        else:
            permanent_stored = {**body, "version": perm_version}
        _permanent[customer_id] = permanent_stored

    assembly_state = stored.get("assembly_state", "unknown")
    reason = (
        f"domain merge ({merge_domain}) for {customer_id}"
        if merge_domain
        else f"full write for {customer_id}"
    )
    await _log(
        request, "cache", "cache_written", reason,
        customer_id=customer_id,
        details={"version": version, "assembly_state": assembly_state, "merge_domain": merge_domain},
    )

    return stored


@app.get("/records/{customer_id}", summary="Read a single entity from the hot cache")
async def read_record(customer_id: str) -> dict[str, Any]:
    async with _lock:
        record = _store.get(customer_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No hot-cache record for {customer_id}")
    return record


@app.get("/records", summary="Snapshot of the hot cache")
async def read_all() -> dict[str, Any]:
    async with _lock:
        return dict(_store)


@app.get("/permanent/{customer_id}", summary="Read a single entity from the permanent store")
async def read_permanent(customer_id: str) -> dict[str, Any]:
    async with _lock:
        record = _permanent.get(customer_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found in permanent store")
    return record


@app.get("/permanent", summary="Snapshot of the permanent store")
async def read_all_permanent() -> dict[str, Any]:
    async with _lock:
        return dict(_permanent)


@app.get("/status", summary="Cache population status for both tiers")
async def status() -> dict[str, Any]:
    async with _lock:
        return {
            "hot_cache": {
                "total_customers": len(_store),
                "customer_ids": list(_store.keys()),
            },
            "permanent_store": {
                "total_customers": len(_permanent),
                "customer_ids": list(_permanent.keys()),
            },
        }


if __name__ == "__main__":
    uvicorn.run("cache_app:app", host="0.0.0.0", port=8012, reload=True)
