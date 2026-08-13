"""
CRM SoR Service — Systems of Record Layer
==========================================
Port: 8010

Mocked CRM system. Owns customer identity records only — no product data.
This separation is intentional: in the target architecture, identity and
subscription data live in distinct SoRs with independent change cadences.

CDC capture is an implementation detail — the SoR exposes a write interface
only. Callers write to the SoR; CDC observes those writes and assembles
canonical records downstream.

On startup, this service emits crm.customer.updated events for every seed
record so that CDC can populate the permanent store before any ACG requests
arrive. This is the correct event-driven approach — the ACG never needs to
drive assembly.

Routes:
  GET   /customers                — list all customers
  GET   /customers/{customer_id}  — raw CRM record
  PATCH /customers/{customer_id}  — update one identity field and capture via CDC
                                    Optional body: {field, new_value}
                                    No body → re-captures current state unchanged
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

CDC_URL = "http://localhost:8011"
LOG_URL = "http://localhost:8015"

_CRM_FILE = Path(__file__).parent.parent / "seed" / "crm.yaml"


def _load_crm_data() -> dict[str, dict[str, Any]]:
    """Load customer records from rules/crm.yaml, keyed by cust_ref."""
    with open(_CRM_FILE) as f:
        raw = yaml.safe_load(f)
    return {c["cust_ref"]: c for c in raw.get("customers", [])}


# Mutable runtime copy — field changes applied by publish-event are persisted here
crm_data: dict[str, dict[str, Any]] = {}


async def _emit_startup_events(
    cdc_http: httpx.AsyncClient,
    log_http: httpx.AsyncClient,
) -> None:
    """Emit crm.customer.updated for every seed record so CDC pre-populates the cache.

    Retries with backoff — CDC may still be starting when this fires. The SoR
    serves requests immediately regardless; this runs in the background.
    """
    for attempt in range(1, 11):
        try:
            for record in crm_data.values():
                resp = await cdc_http.post("/events", json={
                    "raw_record":    record,
                    "source_system": "crm-sor",
                    "event_type":    "crm.customer.updated",
                })
                resp.raise_for_status()
            try:
                await log_http.post("/logs", json={
                    "service": "crm-sor",
                    "action":  "startup_events_emitted",
                    "reason":  f"emitted {len(crm_data)} crm.customer.updated events at startup",
                    "outcome": "success",
                })
            except Exception:
                pass
            return
        except Exception:
            if attempt < 10:
                await asyncio.sleep(0.5 * attempt)
    try:
        await log_http.post("/logs", json={
            "service": "crm-sor",
            "action":  "startup_events_failed",
            "reason":  "failed to emit startup events after 10 attempts — CDC may not be reachable",
            "outcome": "error",
        })
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    for cid, record in _load_crm_data().items():
        crm_data[cid] = copy.deepcopy(record)
    app.state.cdc_http = httpx.AsyncClient(base_url=CDC_URL, timeout=10.0)
    app.state.log_http = httpx.AsyncClient(base_url=LOG_URL, timeout=3.0)
    asyncio.create_task(
        _emit_startup_events(app.state.cdc_http, app.state.log_http)
    )
    yield
    await app.state.cdc_http.aclose()
    await app.state.log_http.aclose()


app = FastAPI(
    title="CRM SoR",
    description="Mocked CRM System of Record — Systems of Record Layer",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def get_cdc_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.cdc_http


async def get_log_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.log_http


class FieldChange(BaseModel):
    """Mutate one identity field before publishing the event."""
    field: str
    new_value: str


@app.get("/customers", summary="List all CRM customers")
async def list_customers() -> list[dict]:
    return [
        {
            "customer_id": k,
            "full_name": f"{v['first_nm']} {v['last_nm']}",
            "email": v.get("email", ""),
        }
        for k, v in crm_data.items()
    ]


@app.get("/customers/{customer_id}", summary="Raw CRM record for a customer")
async def get_customer(customer_id: str) -> dict:
    record = crm_data.get(customer_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found in CRM SoR")
    return record


@app.patch("/customers/{customer_id}", summary="Update a customer identity field")
async def update_customer(
    customer_id: str,
    change: FieldChange | None = None,
    cdc_http: httpx.AsyncClient = Depends(get_cdc_http),
    log_http: httpx.AsyncClient = Depends(get_log_http),
) -> dict:
    """
    Write one identity field to the CRM record. CDC capture of the resulting
    state is an implementation detail of the write — callers have no knowledge
    of CDC. No body re-captures the current state without mutation.
    """
    if customer_id not in crm_data:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

    raw = copy.deepcopy(crm_data[customer_id])
    change_applied: dict | None = None

    if change:
        allowed = {"first_nm", "last_nm", "email", "phone"}
        if change.field not in allowed:
            raise HTTPException(status_code=422, detail=f"field must be one of {allowed}")
        old_val = raw.get(change.field)
        raw[change.field] = change.new_value
        crm_data[customer_id] = raw
        change_applied = {"field": change.field, "old_value": old_val, "new_value": change.new_value}

    try:
        await log_http.post("/logs", json={
            "service": "crm-sor",
            "action": "customer_updated",
            "reason": (
                f"CRM write for {customer_id}"
                + (f" — field {change.field} updated" if change else " — re-capture")
            ),
            "customer_id": customer_id,
            "outcome": "success",
            "details": {"change_applied": change_applied},
        })
    except Exception:
        pass

    cdc_resp = await cdc_http.post("/events", json={
        "raw_record": raw,
        "source_system": "crm-sor",
        "event_type": "crm.customer.updated",
    })
    cdc_resp.raise_for_status()
    cdc_result = cdc_resp.json()

    return {
        "message": "customer record written; CDC capture complete",
        "customer_id": customer_id,
        "change_applied": change_applied,
        "assembly_status": cdc_result.get("assembly_status"),
        "correlation_id": cdc_result.get("correlation_id"),
    }


if __name__ == "__main__":
    uvicorn.run("crm_app:app", host="0.0.0.0", port=8010, reload=True)
