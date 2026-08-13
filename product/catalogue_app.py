"""
Product Catalogue SoR — Systems of Record Layer
================================================
Port: 8014

Owns product master data: SKU definitions, names, categories, base list
prices, available contract terms, bundle definitions, and the product
relationship graph (compatibility, upgrade paths, incompatibilities).

Data is loaded from catalogue.yaml at startup. Price or term changes
are applied in-memory and emitted as product.catalogue.updated events to
CDC, which fans out to re-enrich and re-evaluate discounts for all affected
customers in the context cache.

Routes:
  GET   /products                       — all product definitions
  GET   /products/{sku}                 — single product definition
  PATCH /products/{sku}                 — update price or contract terms;
                                          emits product.catalogue.updated to CDC
  GET   /products/{sku}/compatible      — SKUs structurally compatible with this product
  GET   /products/{sku}/upgrades        — SKUs this product can be upgraded to
  GET   /products/{sku}/incompatible    — SKUs incompatible with this product
  POST  /validate-combination           — check a set of SKUs for structural validity
  POST  /graph/compatible-candidates    — all valid candidate additions for a held portfolio
  GET   /bundles                        — all bundle definitions
  POST  /reload                         — hot-reload catalogue YAML without restart
  GET   /health                         — service status
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import networkx as nx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

CDC_URL = "http://localhost:8011"
LOG_URL = "http://localhost:8015"

_CATALOGUE_FILE = Path(__file__).parent / "catalogue.yaml"

# Module-level state — populated at startup and on /reload
CATALOGUE: dict[str, dict] = {}
BUNDLES:   list[dict]       = []
_GRAPH:    nx.DiGraph | None = None


# ---------------------------------------------------------------------------
# Load / reload
# ---------------------------------------------------------------------------

def _load_catalogue() -> None:
    """Read catalogue.yaml and rebuild CATALOGUE, BUNDLES, and the graph atomically."""
    global _GRAPH

    with open(_CATALOGUE_FILE) as f:
        raw = yaml.safe_load(f)

    # Flat catalogue with API-facing field names
    products: dict[str, dict] = {}
    for p in raw.get("products", []):
        products[p["sku"]] = {
            "sku":                    p["sku"],
            "product_name":           p["name"],
            "product_type":           p["product_type"],
            "list_price_gbp":         p["list_price_gbp"],
            "available_terms_months": p.get("available_terms_months", []),
        }

    bundles: list[dict] = raw.get("bundles", [])

    # Product relationship graph
    G: nx.DiGraph = nx.DiGraph()
    for p in raw.get("products", []):
        G.add_node(p["sku"], node_type="product", **p)
    for b in bundles:
        G.add_node(b["bundle_id"], node_type="bundle", **b)
    for rel in raw.get("relationships", []):
        G.add_edge(rel["from"], rel["to"], relation=rel["type"])
    _validate_no_conflicts(G)

    # Atomic swap
    CATALOGUE.clear()
    CATALOGUE.update(products)
    BUNDLES.clear()
    BUNDLES.extend(bundles)
    _GRAPH = G


def _validate_no_conflicts(G: nx.DiGraph) -> None:
    """Raise ValueError if any product pair is marked both COMPATIBLE_WITH and INCOMPATIBLE_WITH."""
    compatible: set[frozenset] = set()
    incompatible: set[frozenset] = set()
    for u, v, data in G.edges(data=True):
        pair = frozenset([u, v])
        rel = data.get("relation")
        if rel == "COMPATIBLE_WITH":
            compatible.add(pair)
        elif rel == "INCOMPATIBLE_WITH":
            incompatible.add(pair)
    conflicts = compatible & incompatible
    if conflicts:
        pairs = [" <-> ".join(sorted(p)) for p in sorted(conflicts, key=sorted)]
        raise ValueError(
            f"catalogue.yaml contains contradictory relationships: {', '.join(pairs)}"
        )


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _get_graph() -> nx.DiGraph:
    if _GRAPH is None:
        _load_catalogue()
    return _GRAPH


def _neighbours_by_relation(sku: str, relation: str) -> list[str]:
    g = _get_graph()
    return [v for _, v, d in g.out_edges(sku, data=True) if d.get("relation") == relation]


def _is_combination_valid(skus: list[str]) -> tuple[bool, list[str]]:
    """Return (valid, violations) for a proposed set of SKUs."""
    g = _get_graph()
    violations: list[str] = []
    sku_set = set(skus)

    for sku in skus:
        if sku not in g:
            violations.append(f"{sku} is not in the product catalogue")
            continue
        # UPGRADES_TO implies mutual exclusivity
        for _, upgraded_to, data in g.out_edges(sku, data=True):
            if data.get("relation") == "UPGRADES_TO" and upgraded_to in sku_set:
                pair = tuple(sorted([sku, upgraded_to]))
                msg = f"{pair[0]} and {pair[1]} cannot be held simultaneously (upgrade path)"
                if msg not in violations:
                    violations.append(msg)
        for conflict in _neighbours_by_relation(sku, "INCOMPATIBLE_WITH"):
            if conflict in sku_set:
                pair = tuple(sorted([sku, conflict]))
                msg = f"{pair[0]} and {pair[1]} cannot be held simultaneously"
                if msg not in violations:
                    violations.append(msg)

    return (len(violations) == 0, violations)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_catalogue()
    app.state.cdc_http = httpx.AsyncClient(base_url=CDC_URL, timeout=10.0)
    app.state.log_http = httpx.AsyncClient(base_url=LOG_URL, timeout=3.0)
    yield
    await app.state.cdc_http.aclose()
    await app.state.log_http.aclose()


app = FastAPI(
    title="Product Catalogue SoR",
    description="Product master data and relationship graph — Systems of Record Layer",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Flat catalogue endpoints
# ---------------------------------------------------------------------------

class PriceUpdate(BaseModel):
    """Fields that can be updated on a product. At least one must be provided."""
    new_list_price_gbp: float | None = None
    available_terms_months: list[int] | None = None


@app.get("/products", summary="List all product definitions")
async def list_products() -> list[dict]:
    return list(CATALOGUE.values())


@app.get("/products/{sku}", summary="Single product definition by SKU")
async def get_product(sku: str) -> dict:
    product = CATALOGUE.get(sku)
    if not product:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found in catalogue")
    return product


@app.patch("/products/{sku}", summary="Update a product's price or contract terms")
async def update_product(sku: str, update: PriceUpdate, request: Request) -> dict:
    """
    Apply a price or term update to a product. The change is applied in-memory
    and emitted as a product.catalogue.updated event to CDC, which fans out to
    re-enrich and re-evaluate discounts for all customers holding this product.
    """
    product = CATALOGUE.get(sku)
    if not product:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found in catalogue")
    if update.new_list_price_gbp is None and update.available_terms_months is None:
        raise HTTPException(
            status_code=422,
            detail="At least one of new_list_price_gbp or available_terms_months must be provided",
        )

    old_price = product.get("list_price_gbp")
    if update.new_list_price_gbp is not None:
        product["list_price_gbp"] = update.new_list_price_gbp
    if update.available_terms_months is not None:
        product["available_terms_months"] = update.available_terms_months

    try:
        await request.app.state.cdc_http.post("/events", json={
            "raw_record":    product,
            "source_system": "product-catalogue",
            "event_type":    "product.catalogue.updated",
        })
    except Exception:
        pass

    try:
        await request.app.state.log_http.post("/logs", json={
            "service": "product-catalogue",
            "action":  "product_updated",
            "reason":  f"{sku} list_price_gbp changed from {old_price} to {product['list_price_gbp']}",
            "outcome": "success",
            "details": {"sku": sku, "changes": update.model_dump(exclude_none=True)},
        })
    except Exception:
        pass

    return product


@app.get("/bundles", summary="List all bundle definitions")
async def list_bundles() -> list[dict]:
    return BUNDLES


# ---------------------------------------------------------------------------
# Graph endpoints
# ---------------------------------------------------------------------------

@app.get("/products/{sku}/compatible", summary="SKUs structurally compatible with this product")
async def get_compatible(sku: str) -> dict:
    if sku not in CATALOGUE:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found in catalogue")
    return {"sku": sku, "compatible_with": _neighbours_by_relation(sku, "COMPATIBLE_WITH")}


@app.get("/products/{sku}/upgrades", summary="SKUs this product can be upgraded to")
async def get_upgrades(sku: str) -> dict:
    if sku not in CATALOGUE:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found in catalogue")
    return {"sku": sku, "upgrades_to": _neighbours_by_relation(sku, "UPGRADES_TO")}


@app.get("/products/{sku}/incompatible", summary="SKUs incompatible with this product")
async def get_incompatible(sku: str) -> dict:
    if sku not in CATALOGUE:
        raise HTTPException(status_code=404, detail=f"Product {sku} not found in catalogue")
    return {"sku": sku, "incompatible_with": _neighbours_by_relation(sku, "INCOMPATIBLE_WITH")}


class ValidateCombinationRequest(BaseModel):
    skus: list[str]


@app.post("/validate-combination", summary="Check a set of SKUs for structural validity")
async def validate_combination(body: ValidateCombinationRequest) -> dict:
    valid, violations = _is_combination_valid(body.skus)
    return {"valid": valid, "violations": violations}


class CompatibleCandidatesRequest(BaseModel):
    held_skus: list[str]


@app.post(
    "/graph/compatible-candidates",
    summary="All valid candidate additions for a held portfolio",
)
async def compatible_candidates(body: CompatibleCandidatesRequest) -> dict:
    """
    Given the SKUs a customer currently holds, returns all products reachable
    via COMPATIBLE_WITH or UPGRADES_TO edges that the customer does not already
    hold, pre-filtered to those that pass combination validation. Product details
    are included so callers avoid separate lookup calls.
    """
    held_set = set(body.held_skus)
    held_list = sorted(held_set)

    candidate_via: dict[str, list[dict]] = {}
    for sku in held_list:
        for relation in ("COMPATIBLE_WITH", "UPGRADES_TO"):
            for target in _neighbours_by_relation(sku, relation):
                if target not in held_set:
                    candidate_via.setdefault(target, []).append(
                        {"source_sku": sku, "relation": relation}
                    )

    candidates: list[dict] = []
    for sku, via in candidate_via.items():
        valid, violations = _is_combination_valid(held_list + [sku])
        product = CATALOGUE.get(sku)
        candidates.append({
            "sku":                    sku,
            "product_name":           product["product_name"] if product else sku,
            "product_type":           product["product_type"] if product else None,
            "list_price_gbp":         product["list_price_gbp"] if product else None,
            "available_terms_months": product["available_terms_months"] if product else [],
            "compatible_via":         via,
            "valid":                  valid,
            "violations":             violations,
        })

    return {"held_skus": held_list, "candidates": candidates}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@app.post("/reload", summary="Hot-reload catalogue YAML without restarting the service")
async def reload() -> dict:
    _load_catalogue()
    return {
        "message":              "Catalogue reloaded",
        "product_count":        len(CATALOGUE),
        "bundle_count":         len(BUNDLES),
        "relationship_count":   _GRAPH.number_of_edges() if _GRAPH else 0,
    }


@app.get("/health", summary="Service health check")
async def health() -> dict:
    return {
        "status":             "ok",
        "product_count":      len(CATALOGUE),
        "bundle_count":       len(BUNDLES),
        "relationship_count": _GRAPH.number_of_edges() if _GRAPH else 0,
    }


if __name__ == "__main__":
    uvicorn.run("catalogue_app:app", host="0.0.0.0", port=8014, reload=True)
