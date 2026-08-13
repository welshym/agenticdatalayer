"""
Offer Engine — Business Context Layer
======================================
Port: 8016

Evaluates discount policies against customer context packets. Called by the
ACG as part of the retrieval plan, or directly by agents exploring proposals.

Routes:
  POST /discount-summary — body: {customer_id, context}
                           Returns which discount policies are currently firing
                           against a real customer's assembled holdings. Used
                           by the ACG to surface the customer's current discount
                           position — always historical (products must already
                           be purchased to appear in context).

  POST /proposals        — body: {proposed_context, customer_id?, current_context?}
                           Evaluates a hypothetical portfolio. Without customer_id,
                           returns just the proposed discount summary. With
                           customer_id + current_context, returns a before/after
                           comparison and delta so an agent can say "adding X
                           would save you £Y/mo through the bundle discount."

  GET  /rules        — declared discount policies
  POST /reloads      — hot-reload rules/catalogue YAML files without restart
  GET  /health       — service status and loaded rule/product counts
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, model_validator

import commercial_rules

CATALOGUE_URL = "http://localhost:8014"


@asynccontextmanager
async def lifespan(app: FastAPI):
    commercial_rules.reload_policies()
    app.state.catalogue_http = httpx.AsyncClient(base_url=CATALOGUE_URL, timeout=10.0)
    yield
    await app.state.catalogue_http.aclose()


app = FastAPI(
    title="Offer Engine",
    description="Product catalogue graph + commercial discount evaluation — Business Context Layer",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DiscountSummaryRequest(BaseModel):
    customer_id: str
    context: dict


class ProposeRequest(BaseModel):
    """
    Proposal evaluation request.

    Without customer_id: evaluates proposed_context in isolation — useful for
    business teams testing rule changes or agents exploring hypothetical portfolios
    with no specific customer in mind.

    With customer_id + current_context: evaluates both the current and proposed
    states and returns a before/after delta, enabling an agent to tell a customer
    exactly what they would gain or lose by making a change.
    """
    customer_id: str | None = None
    current_context: dict | None = None  # customer's live context — required with customer_id
    proposed_context: dict               # hypothetical portfolio to evaluate

    @model_validator(mode="after")
    def require_current_when_customer_present(self) -> "ProposeRequest":
        if self.customer_id is not None and self.current_context is None:
            raise ValueError("current_context is required when customer_id is provided")
        return self


def _compute_delta(
    current: commercial_rules.DiscountSummary,
    proposed: commercial_rules.DiscountSummary,
) -> dict[str, Any]:
    """Before/after comparison between the current and proposed discount states."""
    charge_delta = round(
        proposed.total_discounted_monthly_gbp - current.total_discounted_monthly_gbp, 2
    )
    saving_delta = round(proposed.total_saving_gbp - current.total_saving_gbp, 2)
    new_policies  = [p for p in proposed.policies_applied if p not in current.policies_applied]
    lost_policies = [p for p in current.policies_applied if p not in proposed.policies_applied]

    if charge_delta < 0:
        net_change = "saving"
    elif charge_delta > 0:
        net_change = "cost_increase"
    else:
        net_change = "neutral"

    return {
        "monthly_charge_delta_gbp": charge_delta,
        "saving_delta_gbp": saving_delta,
        "new_policies_applied": new_policies,
        "lost_policies": lost_policies,
        "net_change": net_change,
    }


def _summary_to_dict(summary: commercial_rules.DiscountSummary) -> dict[str, Any]:
    return {
        "customer_id": summary.customer_id,
        "policies_loaded": summary.policies_loaded,
        "policies_applied": summary.policies_applied,
        "product_discounts": [
            {
                "sku": pd.sku,
                "list_price_gbp": pd.list_price_gbp,
                "applied_policies": pd.applied_policies,
                "total_discount_pct": pd.total_discount_pct,
                "discounted_price_gbp": pd.discounted_price_gbp,
            }
            for pd in summary.product_discounts
        ],
        "total_list_monthly_gbp": summary.total_list_monthly_gbp,
        "total_discounted_monthly_gbp": summary.total_discounted_monthly_gbp,
        "total_saving_gbp": summary.total_saving_gbp,
        "evaluated_at": summary.evaluated_at,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/discount-summary", summary="Discount summary for a customer's current holdings")
async def discount_summary(request: DiscountSummaryRequest) -> dict:
    """
    Returns which discount policies are currently firing against the customer's
    assembled holdings context. Always historical — products must already be
    purchased and present in the context to be evaluated. Called by the ACG as
    part of the retrieval plan to surface the customer's current discount position.
    """
    summary = commercial_rules.evaluate_discounts(
        request.context, customer_id=request.customer_id
    )
    return _summary_to_dict(summary)


@app.post("/proposals", summary="Evaluate a proposed portfolio change and compare resulting discounts")
async def propose(request: ProposeRequest) -> dict:
    """
    Evaluates a hypothetical portfolio against the discount policies.

    Without customer_id: returns the proposed discount summary only. Useful for
    business teams testing how rule changes would affect a scenario.

    With customer_id + current_context: returns current, proposed, and a delta
    block. Agents use this to answer "if you add broadband-fibre-1G, the bundle
    discount kicks in and saves you £8.50/mo" before any purchase is made.
    """
    proposed_summary = commercial_rules.evaluate_discounts(
        request.proposed_context, customer_id=request.customer_id
    )
    result: dict[str, Any] = {
        "customer_id": request.customer_id,
        "proposed":    _summary_to_dict(proposed_summary),
    }

    if request.customer_id and request.current_context is not None:
        current_summary = commercial_rules.evaluate_discounts(
            request.current_context, customer_id=request.customer_id
        )
        result["current"] = _summary_to_dict(current_summary)
        result["delta"]   = _compute_delta(current_summary, proposed_summary)

    return result


@app.get("/rules", summary="Declared discount policies")
async def rules() -> dict:
    policies = commercial_rules._get_policies()
    return {
        "policies": [
            {
                "policy_id": p.policy_id,
                "description": p.description,
                "discount_pct": p.discount_pct,
                "reason": p.reason,
                "rule": dict(p.rule),
            }
            for p in policies
        ]
    }


class CompatibleOffersRequest(BaseModel):
    commercial_state: dict


@app.post("/compatible-offers", summary="Compatible products with per-term savings proposals")
async def compatible_offers(request: CompatibleOffersRequest, req: Request) -> dict:
    """
    Given a customer's assembled commercial_state, returns all products structurally
    compatible with their current holdings that they do not already hold, together
    with a per-contract-term discount delta for each.

    Stateless — accepts commercial_state directly; no customer ID or cache dependency.
    Graph traversal and combination validation are delegated to the Product Catalogue
    service; this endpoint performs only discount computation.
    """
    subscriptions: list[dict] = request.commercial_state.get("subscriptions", [])
    held_list = sorted({s["product_id"] for s in subscriptions if "product_id" in s})

    # Fetch pre-validated compatible candidates from the catalogue graph
    resp = await req.app.state.catalogue_http.post(
        "/graph/compatible-candidates", json={"held_skus": held_list}
    )
    resp.raise_for_status()
    candidates: list[dict] = resp.json()["candidates"]

    # Evaluate current discount position once — baseline for all proposal deltas
    current_summary = commercial_rules.evaluate_discounts(
        {"commercial_state": request.commercial_state}
    )

    results: list[dict] = []
    for candidate in candidates:
        if not candidate["valid"]:
            continue

        candidate_sku = candidate["sku"]
        list_price: float = candidate["list_price_gbp"] or 0.0
        available_terms: list[int] = candidate["available_terms_months"] or [1]

        term_proposals = []
        for term in available_terms:
            proposed_sub = {
                "product_id":           candidate_sku,
                "status":               "active",
                "monthly_charge_gbp":   list_price,
                "contract_term_months": term,
                "product_type":         candidate.get("product_type", ""),
            }
            proposed_summary = commercial_rules.evaluate_discounts({
                "commercial_state": {
                    "subscriptions": subscriptions + [proposed_sub]
                }
            })
            term_proposals.append({
                "contract_term_months": term,
                "monthly_charge_gbp":   list_price,
                "delta":                _compute_delta(current_summary, proposed_summary),
            })

        results.append({
            "sku":                    candidate_sku,
            "product_name":           candidate["product_name"],
            "product_type":           candidate.get("product_type"),
            "list_price_gbp":         list_price,
            "available_terms_months": available_terms,
            "compatible_via":         candidate["compatible_via"],
            "proposals":              term_proposals,
        })

    return {
        "held_skus":           held_list,
        "compatible_products": results,
    }


@app.post("/reloads", summary="Hot-reload rules and catalogue YAML files without restarting the service")
async def reload(request: Request) -> dict:
    policy_ids = commercial_rules.reload_policies()
    catalogue_resp = await request.app.state.catalogue_http.post("/reload")
    catalogue_resp.raise_for_status()
    return {
        "message":        "Rules and catalogue reloaded successfully",
        "catalogue":      catalogue_resp.json(),
        "policies_loaded": policy_ids,
    }


@app.get("/health", summary="Service health and loaded rule counts")
async def health(request: Request) -> dict:
    policies = commercial_rules._get_policies()
    catalogue_resp = await request.app.state.catalogue_http.get("/health")
    catalogue_health = catalogue_resp.json() if catalogue_resp.is_success else {}
    return {
        "status":           "ok",
        "policies_loaded":  len(policies),
        "products_in_graph": catalogue_health.get("product_count", "unknown"),
    }


if __name__ == "__main__":
    uvicorn.run("offer_engine_app:app", host="0.0.0.0", port=8016, reload=True)
