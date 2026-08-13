"""
Billing / Subscription SoR — Systems of Record Layer
=====================================================
Port: 8017

Owns per-customer subscription state: which products a customer holds,
their billing status, effective dates, actual monthly charges (after any
point-of-sale discounts), and contract terms.

This is distinct from the Product Catalogue SoR (port 8014), which owns
master product definitions and list prices. Billing knows what a customer
pays; catalogue knows what the products are.

CDC capture is an implementation detail — the SoR exposes a write interface
only. Callers write to the SoR; CDC observes those writes and assembles
canonical records downstream.

Routes:
  GET   /customers                — list all customers (by cust_ref)
  GET   /customers/{customer_id}  — raw billing record for a customer
  PATCH /customers/{customer_id}  — update or add a subscription line and capture via CDC
                                    Body: {update: {product_sku, new_stat?, new_monthly_charge?}}
                                       or {add: {sku, monthly_charge, contract_term_months?, stat?, eff_from?}}
                                    No body → re-captures current state unchanged
  POST  /recalculate              — bulk recalculate monthly charges for all customers holding
                                    a given SKU, using the current list price from the Product
                                    Catalogue. Emits billing.subscription.updated to CDC for
                                    each affected customer so the cache and permanent record
                                    are updated. Body: {product_sku}
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import commercial_rules

CDC_URL       = "http://localhost:8011"
LOG_URL       = "http://localhost:8015"
CATALOGUE_URL = "http://localhost:8014"

# Single-char SoR status codes to canonical values — mirrors ontology.STATUS_CODES
_STAT_CODES: dict[str, str] = {"A": "active", "S": "suspended", "C": "cancelled"}


def _build_eval_context(billing_record: dict) -> dict:
    """Build a discount-evaluation context from a billing SoR record.

    Passes list_price_gbp as the evaluation price so discount rules are always
    applied against catalogue list prices, not contracted prices. This avoids a
    circular dependency: portfolio-level thresholds must be checked before the
    contracted price is known.
    """
    subs = []
    for sub in billing_record.get("sub_lines", []):
        subs.append({
            "product_id":           sub["sku"],
            "status":               _STAT_CODES.get(sub.get("stat", ""), sub.get("stat", "")),
            "list_price_gbp":       sub.get("list_price_gbp", sub.get("monthly_charge", 0.0)),
            "contract_term_months": sub.get("contract_term_months", 1),
        })
    return {"commercial_state": {"subscriptions": subs}}

_BILLING_FILE = Path(__file__).parent.parent / "seed" / "billing.yaml"


def _load_billing_data() -> dict[str, dict[str, Any]]:
    """Load customer subscription records from rules/billing.yaml, keyed by cust_ref."""
    with open(_BILLING_FILE) as f:
        raw = yaml.safe_load(f)
    return {c["cust_ref"]: c for c in raw.get("customers", [])}


# Mutable runtime copy — status changes applied by publish-event are persisted here
billing_data: dict[str, dict[str, Any]] = {}


async def _emit_startup_events(
    cdc_http: httpx.AsyncClient,
    log_http: httpx.AsyncClient,
) -> None:
    """Emit billing.subscription.updated for every seed record so CDC completes joins.

    Retries with backoff — CRM events may still be in-flight when this fires,
    and CDC may still be starting. Runs in the background so the SoR is
    immediately available.
    """
    for attempt in range(1, 11):
        try:
            for record in billing_data.values():
                resp = await cdc_http.post("/events", json={
                    "raw_record":    record,
                    "source_system": "billing-sor",
                    "event_type":    "billing.subscription.updated",
                })
                resp.raise_for_status()
            try:
                await log_http.post("/logs", json={
                    "service": "billing-sor",
                    "action":  "startup_events_emitted",
                    "reason":  f"emitted {len(billing_data)} billing.subscription.updated events at startup",
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
            "service": "billing-sor",
            "action":  "startup_events_failed",
            "reason":  "failed to emit startup events after 10 attempts — CDC may not be reachable",
            "outcome": "error",
        })
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    for cid, record in _load_billing_data().items():
        billing_data[cid] = copy.deepcopy(record)
    app.state.cdc_http       = httpx.AsyncClient(base_url=CDC_URL,       timeout=10.0)
    app.state.log_http       = httpx.AsyncClient(base_url=LOG_URL,       timeout=3.0)
    app.state.catalogue_http = httpx.AsyncClient(base_url=CATALOGUE_URL, timeout=5.0)
    asyncio.create_task(
        _emit_startup_events(app.state.cdc_http, app.state.log_http)
    )
    yield
    await app.state.cdc_http.aclose()
    await app.state.log_http.aclose()
    await app.state.catalogue_http.aclose()


app = FastAPI(
    title="Billing / Subscription SoR",
    description="Mocked Billing & Subscription System of Record — Systems of Record Layer",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def get_cdc_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.cdc_http


async def get_log_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.log_http


async def get_catalogue_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.catalogue_http


class StatusChangeReason(str, Enum):
    non_payment      = "non_payment"
    customer_request = "customer_request"
    fraud_hold       = "fraud_hold"
    technical_issue  = "technical_issue"
    network_fault    = "network_fault"
    other            = "other"


class SubscriptionUpdate(BaseModel):
    """Patch fields on an existing subscription line. Omitted fields are left unchanged.

    When new_stat is provided, reason_code is required. reason_detail is optional
    free text for additional context (e.g. a case reference or agent note).
    """
    product_sku: str
    new_stat: str | None = None              # A (active) | S (suspended) | C (cancelled)
    reason_code: StatusChangeReason | None = None   # Required when new_stat is set
    reason_detail: str | None = None                # Optional free-text context
    new_monthly_charge: float | None = None  # Inflationary or promotional repricing


class NewSubscription(BaseModel):
    """A new subscription line to add to the customer account.

    Price is never accepted from the caller — it is always fetched from the
    Product Catalogue so the billing system remains the sole authority on what
    a customer is charged.
    """
    sku: str
    stat: str = "A"
    contract_term_months: int = 1
    eff_from: str | None = None  # ISO date; defaults to today


class BillingPatch(BaseModel):
    """
    Billing write operation. Exactly one of 'update' or 'add' should be set.
    An empty body (or neither field) re-captures the current state without mutation.

    When adding a subscription the price is always sourced from the Product
    Catalogue — callers supply the SKU and contract term only.
    """
    update: SubscriptionUpdate | None = None
    add: NewSubscription | None = None


@app.get("/customers", summary="List all customers in Billing SoR")
async def list_customers() -> list[dict]:
    return [
        {"customer_id": k, "subscription_count": len(v.get("sub_lines", []))}
        for k, v in billing_data.items()
    ]


@app.get("/customers/{customer_id}", summary="Raw billing record for a customer")
async def get_customer(customer_id: str) -> dict:
    record = billing_data.get(customer_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found in Billing SoR")
    return record


@app.patch("/customers/{customer_id}", summary="Update or add a subscription line")
async def update_customer(
    customer_id: str,
    patch: BillingPatch | None = None,
    cdc_http:       httpx.AsyncClient = Depends(get_cdc_http),
    log_http:       httpx.AsyncClient = Depends(get_log_http),
    catalogue_http: httpx.AsyncClient = Depends(get_catalogue_http),
) -> dict:
    """
    Write subscription changes to the billing record. Supports:
      - Updating an existing line (stat and/or monthly_charge — omit what should stay unchanged)
      - Adding a new subscription line

    When adding, the list price is always fetched from the Product Catalogue —
    callers supply the SKU and contract term only. Discount rules are re-evaluated
    across the full active portfolio so that adding a product that pushes the
    portfolio total over the £100 threshold correctly updates all existing lines too.

    CDC capture is an implementation detail. No body re-captures current state without mutation.
    """
    if customer_id not in billing_data:
        raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

    raw = copy.deepcopy(billing_data[customer_id])
    change_applied: dict | None = None
    log_reason = "re-capture"

    if patch and patch.update:
        upd = patch.update
        valid_stats = {"A", "S", "C"}
        if upd.new_stat is not None and upd.new_stat not in valid_stats:
            raise HTTPException(status_code=422, detail=f"new_stat must be one of {valid_stats}")
        if upd.new_stat is not None and upd.reason_code is None:
            raise HTTPException(status_code=422, detail="reason_code is required when new_stat is set")
        matched = False
        for sub in raw.get("sub_lines", []):
            if sub["sku"] == upd.product_sku:
                old_vals: dict = {}
                if upd.new_stat is not None:
                    old_vals["stat"] = sub["stat"]
                    sub["stat"] = upd.new_stat
                    sub["reason_code"]   = upd.reason_code.value
                    sub["reason_detail"] = upd.reason_detail
                if upd.new_monthly_charge is not None:
                    old_vals["monthly_charge"] = sub.get("monthly_charge")
                    sub["monthly_charge"] = upd.new_monthly_charge
                matched = True
                change_applied = {
                    "operation": "update",
                    "product_sku": upd.product_sku,
                    "changes": old_vals,
                }
                break
        if not matched:
            raise HTTPException(status_code=404, detail=f"SKU {upd.product_sku} not found for {customer_id}")
        billing_data[customer_id] = raw
        log_reason = f"{upd.product_sku} updated — {change_applied['changes']}"

    elif patch and patch.add:
        new_sub = patch.add
        existing_skus = {s["sku"] for s in raw.get("sub_lines", [])}
        if new_sub.sku in existing_skus:
            raise HTTPException(status_code=409, detail=f"SKU {new_sub.sku} already exists for {customer_id}")

        # Price is always sourced from the catalogue — never from the caller.
        cat_resp = await catalogue_http.get(f"/products/{new_sub.sku}")
        if cat_resp.status_code != 200:
            raise HTTPException(status_code=404, detail=f"SKU {new_sub.sku} not found in Product Catalogue")
        list_price: float | None = cat_resp.json().get("list_price_gbp")
        if list_price is None:
            raise HTTPException(status_code=422, detail=f"Product {new_sub.sku} has no list_price_gbp in catalogue")

        # Insert the new line with list price; discount fields are filled below.
        sub_line: dict = {
            "sku":                  new_sub.sku,
            "stat":                 new_sub.stat,
            "eff_from":             new_sub.eff_from or date.today().isoformat(),
            "list_price_gbp":       list_price,
            "monthly_charge":       list_price,   # overwritten after discount evaluation
            "total_discount_pct":   0.0,
            "applied_discounts":    [],
            "contract_term_months": new_sub.contract_term_months,
        }
        raw.setdefault("sub_lines", []).append(sub_line)

        # Re-evaluate discount rules across the full active portfolio.
        # A new subscription can shift the portfolio total and change discount
        # eligibility for existing lines, so all active lines are updated.
        policies    = commercial_rules._get_policies()
        policy_by_id = {p.policy_id: p for p in policies}
        summary     = commercial_rules.evaluate_discounts(_build_eval_context(raw))
        discount_by_sku = {r.sku: r for r in summary.product_discounts}

        for sub in raw.get("sub_lines", []):
            if sub.get("stat") == "A":
                result = discount_by_sku.get(sub["sku"])
                if result:
                    sub["monthly_charge"]     = result.discounted_price_gbp
                    sub["total_discount_pct"] = result.total_discount_pct
                    sub["applied_discounts"]  = [
                        {
                            "policy_id":    pid,
                            "discount_pct": policy_by_id[pid].discount_pct,
                            "reason":       policy_by_id[pid].reason,
                        }
                        for pid in result.applied_policies
                        if pid in policy_by_id
                    ]

        billing_data[customer_id] = raw
        change_applied = {
            "operation":      "add",
            "sku":            new_sub.sku,
            "list_price_gbp": list_price,
            "monthly_charge": discount_by_sku[new_sub.sku].discounted_price_gbp
                              if new_sub.sku in discount_by_sku else list_price,
        }
        log_reason = (
            f"{new_sub.sku} added — list £{list_price}/mo, "
            f"contracted £{change_applied['monthly_charge']}/mo"
        )

    try:
        await log_http.post("/logs", json={
            "service":     "billing-sor",
            "action":      "subscription_updated" if change_applied else "re_capture",
            "reason":      f"Billing write for {customer_id} — {log_reason}",
            "customer_id": customer_id,
            "outcome":     "success",
            "details":     {"change_applied": change_applied},
        })
    except Exception:
        pass

    cdc_resp = await cdc_http.post("/events", json={
        "raw_record":    raw,
        "source_system": "billing-sor",
        "event_type":    "billing.subscription.updated",
    })
    cdc_resp.raise_for_status()
    cdc_result = cdc_resp.json()

    return {
        "message":         "billing record written; CDC capture complete",
        "customer_id":     customer_id,
        "change_applied":  change_applied,
        "assembly_status": cdc_result.get("assembly_status"),
        "correlation_id":  cdc_result.get("correlation_id"),
    }


class RecalculateRequest(BaseModel):
    """Request body for the bulk billing recalculation endpoint."""
    product_sku: str


@app.post("/recalculate", summary="Bulk recalculate monthly charges from updated product list price")
async def recalculate_from_product(
    body: RecalculateRequest,
    cdc_http:       httpx.AsyncClient = Depends(get_cdc_http),
    log_http:       httpx.AsyncClient = Depends(get_log_http),
    catalogue_http: httpx.AsyncClient = Depends(get_catalogue_http),
) -> dict:
    """
    Fetch the current list_price_gbp for product_sku from the Product Catalogue,
    then for every customer holding that SKU:
      1. Update list_price_gbp on the affected subscription line.
      2. Re-evaluate discount rules for the customer's full active portfolio
         (a price change can shift whether the £100 portfolio threshold fires,
         so all active lines for the customer are recalculated, not just the
         repriced SKU).
      3. Update monthly_charge (contracted price), total_discount_pct, and
         applied_discounts on every active subscription line.
      4. Emit billing.subscription.updated to CDC so the cache and permanent
         record are refreshed.

    Called as a separate step after PATCH /products/{sku} on the Catalogue — the
    catalogue update re-enriches cache metadata (product_name, available_terms etc.)
    while this endpoint propagates the price change into contracted billing records.
    """
    cat_resp = await catalogue_http.get(f"/products/{body.product_sku}")
    if cat_resp.status_code != 200:
        raise HTTPException(
            status_code=404,
            detail=f"Product {body.product_sku} not found in Product Catalogue",
        )
    new_list_price: float | None = cat_resp.json().get("list_price_gbp")
    if new_list_price is None:
        raise HTTPException(status_code=422, detail="Catalogue product has no list_price_gbp")

    # Find all customers with a non-cancelled line for this SKU.
    affected: list[str] = [
        cid for cid, rec in billing_data.items()
        if any(
            sub.get("sku") == body.product_sku and sub.get("stat") != "C"
            for sub in rec.get("sub_lines", [])
        )
    ]

    policies = commercial_rules._get_policies()
    policy_by_id = {p.policy_id: p for p in policies}

    events_emitted = 0
    for cid in affected:
        raw = copy.deepcopy(billing_data[cid])

        # Step 1 — update list_price_gbp for the repriced SKU.
        for sub in raw.get("sub_lines", []):
            if sub.get("sku") == body.product_sku and sub.get("stat") != "C":
                sub["list_price_gbp"] = new_list_price

        # Step 2 — re-evaluate discount rules for the full active portfolio.
        summary = commercial_rules.evaluate_discounts(_build_eval_context(raw))
        discount_by_sku = {r.sku: r for r in summary.product_discounts}

        # Step 3 — update contracted price and discount metadata for all active lines.
        for sub in raw.get("sub_lines", []):
            if sub.get("stat") == "A":
                result = discount_by_sku.get(sub["sku"])
                if result:
                    sub["monthly_charge"]    = result.discounted_price_gbp
                    sub["total_discount_pct"] = result.total_discount_pct
                    sub["applied_discounts"] = [
                        {
                            "policy_id":    pid,
                            "discount_pct": policy_by_id[pid].discount_pct,
                            "reason":       policy_by_id[pid].reason,
                        }
                        for pid in result.applied_policies
                        if pid in policy_by_id
                    ]

        billing_data[cid] = raw

        # Step 4 — emit CDC event so cache and permanent record are refreshed.
        try:
            cdc_resp = await cdc_http.post("/events", json={
                "raw_record":    raw,
                "source_system": "billing-sor",
                "event_type":    "billing.subscription.updated",
            })
            cdc_resp.raise_for_status()
            events_emitted += 1
        except Exception:
            pass

    try:
        await log_http.post("/logs", json={
            "service": "billing-sor",
            "action":  "billing_recalculated",
            "reason":  (
                f"Billing recalculated for SKU {body.product_sku} at list price "
                f"£{new_list_price}/mo — {len(affected)} customer(s) updated with "
                f"re-evaluated discount rules"
            ),
            "outcome": "success",
            "details": {
                "sku":                body.product_sku,
                "new_list_price_gbp": new_list_price,
                "customers_updated":  len(affected),
                "events_emitted":     events_emitted,
            },
        })
    except Exception:
        pass

    return {
        "product_sku":        body.product_sku,
        "new_list_price_gbp": new_list_price,
        "customers_updated":  len(affected),
        "events_emitted":     events_emitted,
    }


if __name__ == "__main__":
    uvicorn.run("billing_app:app", host="0.0.0.0", port=8017, reload=True)
