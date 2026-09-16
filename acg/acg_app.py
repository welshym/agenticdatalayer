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
  GET  /mcp/sse                  — MCP SSE connection endpoint (Bearer JWT required; sub claim bound as caller identity)
  POST /mcp/messages/            — MCP message posting endpoint
"""
from __future__ import annotations

import asyncio
import json
import os
import types as builtin_types
import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from mcp.server import Server as McpServer
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from pydantic_graph import BaseNode, End, Graph, GraphRunContext

import auth
import ontology

CACHE_URL          = "http://localhost:8012"
LOG_URL            = "http://localhost:8015"
OFFER_URL          = "http://localhost:8016"
ACTION_BROKER_URL  = "http://localhost:8018"
OPA_URL            = "http://localhost:8019"

# Bedrock LLM config — the container's task role supplies credentials at runtime.
BEDROCK_MODEL  = os.getenv("BEDROCK_MODEL", "amazon.nova-lite-v1:0")
BEDROCK_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
_bedrock: dict[str, Any] = {}


def _get_bedrock():
    """Lazily create a shared bedrock-runtime client (boto3 is optional at import time)."""
    if "client" not in _bedrock:
        import boto3  # imported lazily so the service still boots without boto3 installed
        _bedrock["client"] = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    return _bedrock["client"]

UI_PATH = Path(__file__).parent / "ui" / "index.html"

# Plan identities and descriptions are declared in ontology.RETRIEVAL_PLANS —
# the ACG reads them rather than maintaining its own copy.
_CONTEXT_PLAN = ontology.RETRIEVAL_PLANS["customer-billing-context-v4"]
_OFFERS_PLAN   = ontology.RETRIEVAL_PLANS["compatible-offers-v1"]

# Caller identity bound at MCP SSE connection time — set from the verified JWT
# sub claim in mcp_sse() before mcp.run() starts. Tool calls always execute in
# the same asyncio task as the SSE handler, so the ContextVar is visible inside
# _call_tool() without any per-call argument.
_SESSION_CALLER: ContextVar[str] = ContextVar("_SESSION_CALLER", default="")


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
            "plan_id":          _CONTEXT_PLAN.plan_id,
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
    graph_structure = _describe_plan(
        _CONTEXT_PLAN.plan_id,
        _CONTEXT_PLAN.description,
        CacheRead.__name__,
        _NODE_TYPES,
    )
    # Merge the ontology's store-chain declaration with the derived graph structure
    graph_structure["task_type"] = _CONTEXT_PLAN.task_type
    graph_structure["stores"]    = ontology.describe_retrieval_plans()[_CONTEXT_PLAN.plan_id]["stores"]
    return graph_structure


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
            "plan_id":          _OFFERS_PLAN.plan_id,
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
    graph_structure = _describe_plan(
        _OFFERS_PLAN.plan_id,
        _OFFERS_PLAN.description,
        CompatibleOffersCacheRead.__name__,
        _COMPATIBLE_OFFERS_NODE_TYPES,
    )
    graph_structure["task_type"] = _OFFERS_PLAN.task_type
    graph_structure["stores"]    = ontology.describe_retrieval_plans()[_OFFERS_PLAN.plan_id]["stores"]
    return graph_structure


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
# Plan executor registry — keyed on task_type from ontology.RETRIEVAL_PLANS.
# Adding a new plan requires: (1) declare it in ontology.RETRIEVAL_PLANS,
# (2) implement its BaseNode subclasses, (3) register it here.
# Route handlers and the MCP dispatcher look up executors by task_type so
# they contain no per-plan logic themselves.
# ---------------------------------------------------------------------------

@dataclass
class PlanExecutor:
    """Binds an ontology-declared retrieval plan to its pydantic-graph implementation."""
    graph: Any                              # Graph[State, Deps, dict]
    entry_node_factory: Any                 # () -> BaseNode  (dataclass, so callable)
    state_factory: Any                      # (customer_id: str) -> State
    deps_factory: Any                       # () -> Deps  (reads _http at call time)


_PLAN_REGISTRY: dict[str, PlanExecutor] = {
    _CONTEXT_PLAN.task_type: PlanExecutor(
        graph=customer_context_graph,
        entry_node_factory=CacheRead,
        state_factory=lambda cid: RetrievalState(customer_id=cid),
        deps_factory=_make_deps,
    ),
    _OFFERS_PLAN.task_type: PlanExecutor(
        graph=compatible_offers_graph,
        entry_node_factory=CompatibleOffersCacheRead,
        state_factory=lambda cid: CompatibleOffersState(customer_id=cid),
        deps_factory=_make_compatible_offers_deps,
    ),
}

# Maps each MCP tool name to the task_type it executes, so _call_tool dispatches
# through the registry rather than branching on tool name.
_MCP_TOOL_TASK_TYPE: dict[str, str] = {
    "get_context":          _CONTEXT_PLAN.task_type,
    "get_compatible_offers": _OFFERS_PLAN.task_type,
}


# ---------------------------------------------------------------------------
# MCP Server — tool description and schema derived from the graph
# ---------------------------------------------------------------------------

# JSON Schema type names for the Python types used in WRITE_ROUTES payload_schema
_PY_TO_JSON_TYPE: dict = {
    str:         "string",
    int:         "integer",
    float:       "number",
    bool:        "boolean",
    (int, float): "number",
}


async def _permitted_callers_from_opa(intent: str) -> list[str]:
    """Return the caller IDs allowed to submit this intent, by querying OPA's data API."""
    try:
        resp = await _http["opa_http"].get("/v1/data/callers")
        callers: dict = resp.json().get("result", {})
        return [
            caller_id
            for caller_id, data in callers.items()
            if intent in data.get("allowed_intents", [])
        ]
    except Exception:
        return []


def _write_tool_schema(intent: str) -> dict:
    """
    Build a flat JSON Schema inputSchema for an MCP write tool from the ontology's
    WRITE_ROUTES declaration. customer_id is added as a required field for
    billing-sor intents; catalogue-sor intents do not require it.
    """
    route = ontology.WRITE_ROUTES[intent]
    needs_customer = route.target_sor == "billing-sor"

    properties: dict[str, dict] = {}
    if needs_customer:
        properties["customer_id"] = {
            "type": "string",
            "description": "Canonical customer identifier.",
        }

    for field_name, spec in route.payload_schema.get("properties", {}).items():
        py_type = spec["type"]
        json_type = _PY_TO_JSON_TYPE.get(py_type, "string")
        properties[field_name] = {"type": json_type}

    required = list(route.payload_schema.get("required", []))
    if needs_customer:
        required = ["customer_id"] + required

    return {"type": "object", "properties": properties, "required": required}


def _format_response_contract(contract: dict) -> str:
    """
    Convert an ontology response contract dict into a concise human-readable
    text block suitable for appending to an MCP tool description.
    """
    lines: list[str] = ["\n\nResponse contract:"]

    lines.append("\nField groups (from cache):")
    for fg_name, fg in contract["field_groups"].items():
        fm = fg["meta_freshness"]
        lines.append(
            f'  • {fg_name} — {fg["description"]} '
            f'TTL {fg["ttl_seconds"]}s, {fg["consistency_class"]} consistency.\n'
            f'    Fields: {", ".join(fg["fields"])}\n'
            f'    {fm["field"]}: "confirmed" ({fm["confirmed"]}) | '
            f'"stale" ({fm["stale"]}) | "unknown" ({fm["unknown"]})'
        )

    lines.append("\nassembly_state values:")
    for state, desc in contract["assembly_states"].items():
        lines.append(f'  • "{state}" — {desc}')
    lines.append(contract["control_fields"]["assembly_state"]["note"])

    if contract["enrichment_outputs"]:
        lines.append("\nEnrichment outputs:")
        for store_name, enrichment in contract["enrichment_outputs"].items():
            for field_name, field_schema in enrichment["output_fields"].items():
                lines.append(f'  • {field_name} — {field_schema["description"]}')

    return "\n".join(lines)


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
    context_contract = ontology.describe_response_contract(_CONTEXT_PLAN.plan_id)
    offers_contract  = ontology.describe_response_contract(_OFFERS_PLAN.plan_id)

    read_tools = [
        Tool(
            name="get_context",
            description=_CONTEXT_PLAN.description + _format_response_contract(context_contract),
            inputSchema=_customer_id_schema,
        ),
        Tool(
            name="get_compatible_offers",
            description=_OFFERS_PLAN.description + _format_response_contract(offers_contract),
            inputSchema=_customer_id_schema,
        ),
    ]

    # One write tool per declared write route. Caller identity comes from the
    # verified JWT bound at SSE connection time — not from tool arguments.
    # Permitted callers are fetched from OPA once and filtered per intent.
    write_tools = []
    for intent, route in ontology.WRITE_ROUTES.items():
        permitted = await _permitted_callers_from_opa(intent)
        callers_str = ", ".join(permitted) if permitted else "none registered"
        write_tools.append(Tool(
            name=intent,
            description=(
                f"{route.description}\n\n"
                f"Permitted callers: {callers_str}.\n"
                f"Target SoR: {route.target_sor}."
            ),
            inputSchema=_write_tool_schema(intent),
        ))

    return read_tools + write_tools


@mcp.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
    # Write intent — forward to Action Broker using the session-bound caller identity
    if name in ontology.WRITE_ROUTES:
        caller_id = _SESSION_CALLER.get()
        if not caller_id:
            raise ValueError("No caller identity bound to this session — JWT required at SSE connect")
        token = auth.mint_token(caller_id)
        customer_id = arguments.get("customer_id")
        payload = {k: v for k, v in arguments.items() if k != "customer_id"}
        resp = await _http["action_broker_http"].post(
            "/submit-intent",
            json={"intent": name, "customer_id": customer_id, "payload": payload},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return [TextContent(type="text", text=json.dumps(resp.json()))]

    # Read intent — dispatch through the plan registry
    task_type = _MCP_TOOL_TASK_TYPE.get(name)
    if task_type is None:
        raise ValueError(f"Unknown tool: {name}")
    executor = _PLAN_REGISTRY[task_type]
    run_result = await executor.graph.run(
        executor.entry_node_factory(),
        state=executor.state_factory(arguments["customer_id"]),
        deps=executor.deps_factory(),
    )
    return [TextContent(type="text", text=json.dumps(run_result.output))]


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _http["cache_http"]          = httpx.AsyncClient(base_url=CACHE_URL,         timeout=10.0)
    _http["log_http"]            = httpx.AsyncClient(base_url=LOG_URL,           timeout=3.0)
    _http["offer_http"]          = httpx.AsyncClient(base_url=OFFER_URL,         timeout=10.0)
    _http["action_broker_http"]  = httpx.AsyncClient(base_url=ACTION_BROKER_URL, timeout=15.0)
    _http["opa_http"]            = httpx.AsyncClient(base_url=OPA_URL,           timeout=5.0)
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
    executor = _PLAN_REGISTRY[_CONTEXT_PLAN.task_type]
    run_result = await executor.graph.run(
        executor.entry_node_factory(),
        state=executor.state_factory(customer_id),
        deps=executor.deps_factory(),
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
    executor = _PLAN_REGISTRY[_OFFERS_PLAN.task_type]
    run_result = await executor.graph.run(
        executor.entry_node_factory(),
        state=executor.state_factory(customer_id),
        deps=executor.deps_factory(),
    )
    return run_result.output


class ChatRequest(BaseModel):
    customer_id: str
    question: str


_CHAT_SYSTEM = (
    "You are the Business Context Layer assistant for a telecom/retail operator. "
    "Answer the user's question about the given customer using ONLY the CONTEXT block below. "
    "The context is the authoritative, governed record assembled by the platform. "
    "Be concise and specific: cite product names, contract terms, prices and savings where relevant. "
    "If the answer is not present in the context, say you don't have that information — never invent data. "
    "Do not mention JSON, fields, or that you were given a context block; answer naturally."
)


@app.post("/chat", summary="Grounded natural-language chat over a customer's assembled context")
async def chat(req: ChatRequest, _token: dict = Depends(_require_jwt)) -> dict[str, Any]:
    """
    Retrieves the customer's governed context (and compatible offers), grounds a Bedrock
    LLM on it via the Converse API, and returns a natural-language answer. The retrieval
    path is the same governed two-tier plan used by /context and /compatible-offers.
    """
    # 1. Assemble grounding context via the governed retrieval plans (raises 404 if unknown)
    context = await get_context(req.customer_id, _token)
    try:
        offers = await get_compatible_offers(req.customer_id, _token)
    except HTTPException:
        offers = {}
    except Exception:
        offers = {}

    grounding = {
        "customer_id":      context.get("customer_id"),
        "profile":          context.get("profile"),
        "commercial_state": context.get("commercial_state"),
        "discount_summary": context.get("discount_summary"),
        "assembly_state":   context.get("assembly_state"),
        "compatible_offers": offers.get("compatible_products", []),
        "held_skus":        offers.get("held_skus", []),
    }
    prompt = (
        f"CONTEXT (customer {req.customer_id}):\n"
        f"{json.dumps(grounding, default=str, indent=2)}\n\n"
        f"QUESTION: {req.question}"
    )

    # 2. Call Bedrock Converse (sync SDK → offloaded to a thread)
    def _invoke() -> str:
        client = _get_bedrock()
        resp = client.converse(
            modelId=BEDROCK_MODEL,
            system=[{"text": _CHAT_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 500, "temperature": 0.2, "topP": 0.9},
        )
        return resp["output"]["message"]["content"][0]["text"].strip()

    try:
        answer = await asyncio.to_thread(_invoke)
    except Exception as exc:
        # Surface a clean error so the UI can fall back to the deterministic engine
        raise HTTPException(status_code=502, detail=f"LLM unavailable: {type(exc).__name__}: {exc}")

    # Fire-and-forget audit log
    try:
        await _http["log_http"].post("/logs", json={
            "service":     "acg",
            "action":      "chat_answered",
            "reason":      f"LLM chat answered for {req.customer_id}",
            "customer_id": req.customer_id,
            "outcome":     "success",
            "details":     {"model": BEDROCK_MODEL},
        })
    except Exception:
        pass

    return {
        "answer":      answer,
        "model":       BEDROCK_MODEL,
        "source":      "bedrock",
        "customer_id": req.customer_id,
        "grounded_on": {
            "assembly_state":   grounding["assembly_state"],
            "compatible_count": len(grounding["compatible_offers"]),
        },
    }


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
    """
    MCP SSE connection endpoint — agent runtimes connect here.

    Requires a Bearer JWT in the Authorization header. The verified sub claim
    is bound to this connection as the caller identity for all tool calls made
    on it. Write tools (add_subscription, cancel_subscription, etc.) use this
    identity to acquire an Action Broker token; no caller_id argument is needed
    in tool inputs.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return Response(status_code=401, content="Bearer token required for MCP SSE connection")
    try:
        decoded = auth.verify_token(auth_header[len("Bearer "):])
    except Exception as exc:
        return Response(status_code=401, content=f"Invalid token: {exc}")

    _SESSION_CALLER.set(decoded["sub"])

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
