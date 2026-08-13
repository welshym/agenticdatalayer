"""
Agent Context Gateway Service — Agentic Layer
=============================================
Port: 8013

Single integration point between agents and the Business Context Layer.
The retrieval plan is a pydantic-graph Graph — the same object drives execution,
Mermaid diagram generation, REST introspection, and the MCP tool schema.
No LLM is involved in step selection; all transitions are determined by the
return type of each node's run() method.

Routes:
  GET  /                             — Agent UI (index.html)
  GET  /context/{customer_id}        — Execute two-tier cache retrieval plan
  GET  /retrieval-plan               — Serialised graph description (nodes + edges)
  GET  /retrieval-plan/mermaid   — Mermaid flowchart of the retrieval graph
  GET  /ontology                 — Introspect declared assembly spec
  GET  /cache-status             — Current cache population
  GET  /mcp/sse                  — MCP SSE connection endpoint (clients connect here)
  POST /mcp/messages/            — MCP message posting endpoint
"""
from __future__ import annotations

import json
import types as builtin_types
import typing
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server import Server as McpServer
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

import auth
import ontology

CACHE_URL = "http://localhost:8012"
LOG_URL   = "http://localhost:8015"
OFFER_URL = "http://localhost:8016"

UI_PATH = Path(__file__).parent / "ui" / "index.html"

PLAN_ID = "customer-billing-context-v4"
PLAN_DESCRIPTION = (
    "Two-tier cache-first retrieval of fully-assembled canonical customer context. "
    "Reads the hot cache (Tier 2/3) first; on a miss falls back to the permanent store (Tier 1). "
    "Known customers are always present in the permanent store — populated at startup by the SoRs "
    "emitting events through the CDC assembly pipeline. "
    "A permanent-store miss means the customer is genuinely unknown: the ACG logs it and returns 404. "
    "The ACG is a pure reader — it never drives assembly or publishes events. "
    "Partial records (assembly_state != complete) are returned transparently with missing_domains."
)

COMPATIBLE_OFFERS_PLAN_ID = "compatible-offers-v1"
COMPATIBLE_OFFERS_PLAN_DESCRIPTION = (
    "Two-tier cache-first retrieval of compatible product offers for a customer. "
    "Reads the assembled customer context from the hot cache (Tier 2/3), falling back to the "
    "permanent store (Tier 1) on a miss. Passes the customer's commercial_state to the Offer "
    "Engine, which walks the product graph from each held SKU to find structurally compatible "
    "and upgrade-path products not yet held, then evaluates the discount policy delta for each "
    "available contract term. Returns compatible products with per-term savings proposals."
)


# ---------------------------------------------------------------------------
# Typed state and dependencies
# ---------------------------------------------------------------------------

@dataclass
class RetrievalState:
    customer_id: str
    cache_hit: bool = False
    retrieval_source: str = "hot_cache"  # hot_cache | permanent_store
    cache_record: dict = field(default_factory=dict)


@dataclass
class RetrievalDeps:
    cache_http: httpx.AsyncClient
    log_http: httpx.AsyncClient


@dataclass
class CompatibleOffersState:
    customer_id: str
    cache_hit: bool = False
    retrieval_source: str = "hot_cache"
    cache_record: dict = field(default_factory=dict)
    compatible_offers: dict = field(default_factory=dict)


@dataclass
class CompatibleOffersDeps:
    cache_http: httpx.AsyncClient
    offer_http: httpx.AsyncClient
    log_http: httpx.AsyncClient


def _with_freshness(group: dict | None) -> dict | None:
    """Compute and inject a freshness flag into a field group's _meta at read time."""
    if not group:
        return group
    meta = group.get("_meta", {})
    assembled_at_str = meta.get("assembled_at")
    ttl_seconds = meta.get("ttl_seconds")
    if assembled_at_str and ttl_seconds:
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(assembled_at_str)
        ).total_seconds()
        freshness = "confirmed" if age <= ttl_seconds else "stale"
    else:
        freshness = "unknown"
    return {**group, "_meta": {**meta, "freshness": freshness}}


# ---------------------------------------------------------------------------
# Retrieval graph nodes
# Each node's run() return type declares the possible transitions — these are
# the edges. pydantic-graph reads the annotations to build and diagram the graph.
# ---------------------------------------------------------------------------

@dataclass
class CacheRead(BaseNode[RetrievalState, RetrievalDeps, dict]):
    """Read canonical record from the hot cache (Tier 2/3)."""

    async def run(
        self, ctx: GraphRunContext[RetrievalState, RetrievalDeps]
    ) -> BuildResponse | CacheReadPermanent:
        resp = await ctx.deps.cache_http.get(f"/records/{ctx.state.customer_id}")
        if resp.status_code == 200:
            ctx.state.cache_record = resp.json()
            ctx.state.cache_hit = True
            ctx.state.retrieval_source = "hot_cache"
            return BuildResponse()
        if resp.status_code != 404:
            resp.raise_for_status()
        ctx.state.cache_hit = False
        return CacheReadPermanent()


@dataclass
class CacheReadPermanent(BaseNode[RetrievalState, RetrievalDeps, dict]):
    """Read from the permanent store (Tier 1) on a hot-cache miss.

    A miss here means the customer is genuinely unknown — the SoRs have never
    emitted an event for them. The ACG logs it and returns 404; it never drives
    assembly.
    """

    async def run(
        self, ctx: GraphRunContext[RetrievalState, RetrievalDeps]
    ) -> BuildResponse:
        resp = await ctx.deps.cache_http.get(f"/permanent/{ctx.state.customer_id}")
        if resp.status_code == 200:
            ctx.state.cache_record = resp.json()
            ctx.state.retrieval_source = "permanent_store"
            return BuildResponse()
        if resp.status_code != 404:
            resp.raise_for_status()
        # Genuine miss — log it and surface a clean 404 to the caller
        try:
            await ctx.deps.log_http.post("/logs", json={
                "service":     "acg",
                "action":      "permanent_miss",
                "reason":      f"customer {ctx.state.customer_id} not found in permanent store",
                "customer_id": ctx.state.customer_id,
                "outcome":     "not_found",
            })
        except Exception:
            pass
        raise HTTPException(
            status_code=404,
            detail=f"Customer {ctx.state.customer_id} not found",
        )


@dataclass
class BuildResponse(BaseNode[RetrievalState, RetrievalDeps, dict]):
    """Assemble context packet from the enriched cache record."""

    async def run(
        self, ctx: GraphRunContext[RetrievalState, RetrievalDeps]
    ) -> End[dict]:
        record = ctx.state.cache_record
        commercial = record.get("commercial_state")
        # discount_summary is written into commercial_state by CDC at assembly time
        discount_summary = (commercial or {}).get("discount_summary", {})
        return End({
            "plan_id":          PLAN_ID,
            "cache_hit":        ctx.state.cache_hit,
            "retrieval_source": ctx.state.retrieval_source,
            "customer_id":      record.get("customer_id"),
            "profile":          _with_freshness(record.get("profile")),
            "commercial_state": _with_freshness(commercial),
            "assembled_at":     record.get("assembled_at"),
            "version":          record.get("version"),
            "provenance":       record.get("provenance", {}),
            "assembly_state":   record.get("assembly_state", "complete"),
            "missing_domains":  record.get("missing_domains", []),
            "discount_summary": discount_summary,
        })


# ---------------------------------------------------------------------------
# Graph — node types list is the single source used for construction,
# introspection, and Mermaid generation
# ---------------------------------------------------------------------------

_NODE_TYPES: list[type[BaseNode]] = [
    CacheRead, CacheReadPermanent, BuildResponse,
]

customer_context_graph: Graph[RetrievalState, RetrievalDeps, dict] = Graph(
    nodes=_NODE_TYPES
)


def _describe_plan(
    plan_id: str,
    description: str,
    entry_node_name: str,
    node_types: list[type[BaseNode]],
) -> dict:
    """
    Build a serialisable plan description from node docstrings and run() return type
    annotations. Edges are derived from the union return types — no separate manifest
    to maintain. Used by retrieval-plan endpoints and MCP tool schemas.
    """
    nodes = [
        {"name": tp.__name__, "description": (tp.__doc__ or "").strip()}
        for tp in node_types
    ]
    edges = []
    for tp in node_types:
        hints = typing.get_type_hints(tp.run)
        return_hint = hints.get("return")
        if return_hint is None:
            continue
        # Decompose union types — handles both Union[A, B] and A | B syntax
        if (
            typing.get_origin(return_hint) is typing.Union
            or isinstance(return_hint, builtin_types.UnionType)
        ):
            args = typing.get_args(return_hint)
        else:
            args = (return_hint,)
        for arg in args:
            # Skip End[T] — terminal, not an edge to another node
            if arg is End or typing.get_origin(arg) is End:
                continue
            if isinstance(arg, type) and issubclass(arg, BaseNode):
                edges.append({"from": tp.__name__, "to": arg.__name__})
    return {
        "plan_id":     plan_id,
        "description": description,
        "entry_node":  entry_node_name,
        "nodes":       nodes,
        "edges":       edges,
    }


def _describe_graph() -> dict:
    return _describe_plan(PLAN_ID, PLAN_DESCRIPTION, CacheRead.__name__, _NODE_TYPES)


# ---------------------------------------------------------------------------
# Compatible offers retrieval graph
# CacheRead → FetchCompatibleOffers | PermanentRead → FetchCompatibleOffers → BuildResponse
# ---------------------------------------------------------------------------

@dataclass
class CompatibleOffersCacheRead(BaseNode[CompatibleOffersState, CompatibleOffersDeps, dict]):
    """Read assembled canonical record from the hot cache (Tier 2/3)."""

    async def run(
        self, ctx: GraphRunContext[CompatibleOffersState, CompatibleOffersDeps]
    ) -> "FetchCompatibleOffers | CompatibleOffersPermanentRead":
        resp = await ctx.deps.cache_http.get(f"/records/{ctx.state.customer_id}")
        if resp.status_code == 200:
            ctx.state.cache_record = resp.json()
            ctx.state.cache_hit = True
            ctx.state.retrieval_source = "hot_cache"
            return FetchCompatibleOffers()
        if resp.status_code != 404:
            resp.raise_for_status()
        ctx.state.cache_hit = False
        return CompatibleOffersPermanentRead()


@dataclass
class CompatibleOffersPermanentRead(BaseNode[CompatibleOffersState, CompatibleOffersDeps, dict]):
    """Read from the permanent store (Tier 1) on a hot-cache miss.

    A miss here means the customer is genuinely unknown. The ACG logs it and returns 404.
    """

    async def run(
        self, ctx: GraphRunContext[CompatibleOffersState, CompatibleOffersDeps]
    ) -> "FetchCompatibleOffers":
        resp = await ctx.deps.cache_http.get(f"/permanent/{ctx.state.customer_id}")
        if resp.status_code == 200:
            ctx.state.cache_record = resp.json()
            ctx.state.retrieval_source = "permanent_store"
            return FetchCompatibleOffers()
        if resp.status_code != 404:
            resp.raise_for_status()
        try:
            await ctx.deps.log_http.post("/logs", json={
                "service":     "acg",
                "action":      "permanent_miss",
                "reason":      f"customer {ctx.state.customer_id} not found in permanent store",
                "customer_id": ctx.state.customer_id,
                "outcome":     "not_found",
            })
        except Exception:
            pass
        raise HTTPException(
            status_code=404,
            detail=f"Customer {ctx.state.customer_id} not found",
        )


@dataclass
class FetchCompatibleOffers(BaseNode[CompatibleOffersState, CompatibleOffersDeps, dict]):
    """Pass the customer's commercial_state to the Offer Engine and collect compatible
    products with per-term savings proposals via the product graph."""

    async def run(
        self, ctx: GraphRunContext[CompatibleOffersState, CompatibleOffersDeps]
    ) -> "BuildCompatibleOffersResponse":
        commercial = ctx.state.cache_record.get("commercial_state")
        if not commercial:
            # Partial record — no commercial_state yet; return empty result
            ctx.state.compatible_offers = {"held_skus": [], "compatible_products": []}
            return BuildCompatibleOffersResponse()
        resp = await ctx.deps.offer_http.post(
            "/compatible-offers", json={"commercial_state": commercial}
        )
        resp.raise_for_status()
        ctx.state.compatible_offers = resp.json()
        return BuildCompatibleOffersResponse()


@dataclass
class BuildCompatibleOffersResponse(BaseNode[CompatibleOffersState, CompatibleOffersDeps, dict]):
    """Assemble the final compatible offers context packet."""

    async def run(
        self, ctx: GraphRunContext[CompatibleOffersState, CompatibleOffersDeps]
    ) -> End[dict]:
        record = ctx.state.cache_record
        return End({
            "plan_id":          COMPATIBLE_OFFERS_PLAN_ID,
            "cache_hit":        ctx.state.cache_hit,
            "retrieval_source": ctx.state.retrieval_source,
            "customer_id":      record.get("customer_id"),
            "assembly_state":   record.get("assembly_state", "complete"),
            **ctx.state.compatible_offers,
        })


_COMPATIBLE_OFFERS_NODE_TYPES: list[type[BaseNode]] = [
    CompatibleOffersCacheRead,
    CompatibleOffersPermanentRead,
    FetchCompatibleOffers,
    BuildCompatibleOffersResponse,
]

compatible_offers_graph: Graph[CompatibleOffersState, CompatibleOffersDeps, dict] = Graph(
    nodes=_COMPATIBLE_OFFERS_NODE_TYPES
)


def _describe_compatible_offers_graph() -> dict:
    return _describe_plan(
        COMPATIBLE_OFFERS_PLAN_ID,
        COMPATIBLE_OFFERS_PLAN_DESCRIPTION,
        CompatibleOffersCacheRead.__name__,
        _COMPATIBLE_OFFERS_NODE_TYPES,
    )


# ---------------------------------------------------------------------------
# Shared HTTP clients — populated in lifespan, used by graph and MCP handler
# ---------------------------------------------------------------------------

_http: dict[str, httpx.AsyncClient] = {}

# ---------------------------------------------------------------------------
# JWT dependency — all API routes require a valid Bearer token.
# MCP transport endpoints (/mcp/sse, /mcp/messages/) and the UI (/) are exempt
# because MCP auth is handled at the MCP protocol layer, and the UI acquires
# its own token on startup via POST /token on the Action Broker.
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


async def _require_jwt(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    """Validate the Bearer JWT and return the decoded payload."""
    try:
        return auth.verify_token(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {exc}")


def _make_deps() -> RetrievalDeps:
    return RetrievalDeps(
        cache_http=_http["cache_http"],
        log_http=_http["log_http"],
    )


def _make_compatible_offers_deps() -> CompatibleOffersDeps:
    return CompatibleOffersDeps(
        cache_http=_http["cache_http"],
        offer_http=_http["offer_http"],
        log_http=_http["log_http"],
    )


# ---------------------------------------------------------------------------
# MCP Server — tool description and schema derived from the graph
# ---------------------------------------------------------------------------

mcp = McpServer("agent-context-gateway")
_sse_transport = SseServerTransport("/mcp/messages/")


@mcp.list_tools()
async def _list_tools() -> list[Tool]:
    _customer_id_schema = {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string", "description": "Canonical customer identifier"}
        },
        "required": ["customer_id"],
    }
    return [
        Tool(name="get_context",           description=PLAN_DESCRIPTION,                   inputSchema=_customer_id_schema),
        Tool(name="get_compatible_offers",  description=COMPATIBLE_OFFERS_PLAN_DESCRIPTION, inputSchema=_customer_id_schema),
    ]


@mcp.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    customer_id = arguments["customer_id"]
    if name == "get_context":
        run_result = await customer_context_graph.run(
            CacheRead(),
            state=RetrievalState(customer_id=customer_id),
            deps=_make_deps(),
        )
        result = run_result.output
    elif name == "get_compatible_offers":
        run_result = await compatible_offers_graph.run(
            CompatibleOffersCacheRead(),
            state=CompatibleOffersState(customer_id=customer_id),
            deps=_make_compatible_offers_deps(),
        )
        result = run_result.output
    else:
        raise ValueError(f"Unknown tool: {name}")
    return [TextContent(type="text", text=json.dumps(result))]


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _http["cache_http"] = httpx.AsyncClient(base_url=CACHE_URL, timeout=10.0)
    _http["log_http"]   = httpx.AsyncClient(base_url=LOG_URL,   timeout=3.0)
    _http["offer_http"] = httpx.AsyncClient(base_url=OFFER_URL, timeout=10.0)
    yield
    for client in _http.values():
        await client.aclose()
    _http.clear()


app = FastAPI(
    title="Agent Context Gateway",
    description="ACG — Agentic Layer integration point for the Business Context Layer",
    version="3.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def serve_ui() -> FileResponse:
    return FileResponse(UI_PATH)


@app.get("/retrieval-plan", summary="Serialised retrieval graph (nodes + edges)")
async def retrieval_plan(_token: dict = Depends(_require_jwt)) -> dict:
    return _describe_graph()


@app.get("/retrieval-plan/mermaid", summary="Mermaid flowchart of the retrieval graph")
async def retrieval_plan_mermaid(_token: dict = Depends(_require_jwt)) -> PlainTextResponse:
    return PlainTextResponse(
        customer_context_graph.mermaid_code(start_node=CacheRead()),
        media_type="text/plain",
    )


@app.get("/ontology", summary="Introspect declared assembly spec")
async def get_ontology(_token: dict = Depends(_require_jwt)) -> dict:
    return ontology.describe_spec()


@app.get("/context/{customer_id}", summary="Execute retrieval plan for a customer")
async def get_context(customer_id: str, _token: dict = Depends(_require_jwt)) -> dict[str, Any]:
    """
    Executes the customer context retrieval graph.
    Partial records (assembly_state != complete) are returned transparently.
    """
    run_result = await customer_context_graph.run(
        CacheRead(),
        state=RetrievalState(customer_id=customer_id),
        deps=_make_deps(),
    )
    result = run_result.output

    # Fire-and-forget log — failure must never interrupt the response path
    try:
        assembly_state = result.get("assembly_state", "complete")
        missing = result.get("missing_domains", [])
        await _http["log_http"].post("/logs", json={
            "service":     "acg",
            "action":      "partial_served" if assembly_state != "complete" else "context_served",
            "reason": (
                f"context served for {customer_id} — assembly_state: {assembly_state}"
                + (f" — missing: {missing}" if missing else "")
            ),
            "customer_id": customer_id,
            "outcome":     "success" if assembly_state == "complete" else "partial",
            "details": {
                "cache_hit":       result.get("cache_hit"),
                "assembly_state":  assembly_state,
                "missing_domains": missing,
                "version":         result.get("version"),
            },
        })
    except Exception:
        pass

    return result


@app.get("/compatible-offers/{customer_id}", summary="Execute compatible offers retrieval plan for a customer")
async def get_compatible_offers(customer_id: str, _token: dict = Depends(_require_jwt)) -> dict[str, Any]:
    """
    Executes the compatible offers retrieval graph. Reads the customer's assembled
    context from the cache, then delegates to the Offer Engine to walk the product
    graph and evaluate per-term discount proposals for each compatible product.
    """
    run_result = await compatible_offers_graph.run(
        CompatibleOffersCacheRead(),
        state=CompatibleOffersState(customer_id=customer_id),
        deps=_make_compatible_offers_deps(),
    )
    return run_result.output


@app.get("/compatible-offers-plan", summary="Serialised compatible offers retrieval graph (nodes + edges)")
async def compatible_offers_plan(_token: dict = Depends(_require_jwt)) -> dict:
    return _describe_compatible_offers_graph()


@app.get("/cache-status", summary="Cache population status (both tiers)")
async def cache_status(_token: dict = Depends(_require_jwt)) -> dict:
    resp = await _http["cache_http"].get("/status")
    resp.raise_for_status()
    return resp.json()


@app.get("/mcp/sse", include_in_schema=False)
async def mcp_sse(request: Request) -> None:
    """MCP SSE connection endpoint — agent runtimes connect here."""
    async with _sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp.run(streams[0], streams[1], mcp.create_initialization_options())


@app.post("/mcp/messages/", include_in_schema=False)
async def mcp_messages(request: Request) -> None:
    """MCP message posting endpoint — used by SSE transport clients."""
    await _sse_transport.handle_post_message(
        request.scope, request.receive, request._send
    )


if __name__ == "__main__":
    uvicorn.run("acg_app:app", host="0.0.0.0", port=8013, reload=True)
