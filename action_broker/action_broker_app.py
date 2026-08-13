"""
Action Broker — Agentic Layer write governance
==============================================
Port: 8018

Sits between agent callers and the Systems of Record layer. Every write
intent from an agent must pass through the Action Broker; direct SoR writes
bypass all governance and are not permitted in the architecture.

Enforcement chain on POST /submit-intent:
  1. JWT verification             — validate Bearer token; derive caller_id from sub claim
  2. Permission check             — is this intent in the caller's allowed list?
  3. Route resolution             — which SoR and endpoint handles this intent?
  4. Payload schema validation    — are all required fields present and typed correctly?
  5. SoR write                    — forward the request to the target SoR
  6. Audit record                 — immutable append-only record of every decision
  7. Response                     — SoR result + audit_id returned to caller

Routes:
  POST /token                    — Issue a JWT for a registered demo client
  POST /submit-intent            — Execute a governed write intent (JWT required)
  GET  /permissions/{caller_id}  — Introspect what a caller is permitted to do (JWT required)
  GET  /permissions              — All declared caller permissions (JWT required)
  GET  /write-routes             — All declared write routes (JWT required)
  GET  /audit                    — Recent audit log — last 200 entries (JWT required)
  GET  /health                   — Service status (no auth)
"""
from __future__ import annotations

import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

import auth
import ontology
from ontology import AGENT_PERMISSIONS, WRITE_ROUTES, AgentPermission, WriteRoute

BILLING_URL   = "http://localhost:8017"
CATALOGUE_URL = "http://localhost:8014"
LOG_URL       = "http://localhost:8015"

# Immutable append-only audit deque — last 200 entries kept in memory
_audit_log: deque[dict] = deque(maxlen=200)

# Shared HTTP clients populated at startup
_http: dict[str, httpx.AsyncClient] = {}

# ---------------------------------------------------------------------------
# JWT dependency
# ---------------------------------------------------------------------------

_bearer = HTTPBearer()


async def _require_jwt(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    """FastAPI dependency — validates the Bearer JWT and returns the decoded payload."""
    try:
        return auth.verify_token(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {exc}")


# ---------------------------------------------------------------------------
# Payload schema validation
# Schema dicts in ontology are {required: [...], properties: {field: {type: T}}}
# ---------------------------------------------------------------------------

def _validate_payload(payload: dict, schema: dict) -> list[str]:
    """
    Validate payload against a write-route schema.
    Returns a list of violation strings; empty means valid.
    """
    violations: list[str] = []
    required: list[str] = schema.get("required", [])
    props: dict = schema.get("properties", {})

    for field in required:
        if field not in payload:
            violations.append(f"missing required field: '{field}'")

    for field, spec in props.items():
        if field not in payload:
            continue
        expected_type = spec.get("type")
        if expected_type and not isinstance(payload[field], expected_type):
            actual = type(payload[field]).__name__
            exp_name = (
                expected_type.__name__
                if isinstance(expected_type, type)
                else str(expected_type)
            )
            violations.append(
                f"field '{field}': expected {exp_name}, got {actual}"
            )

    unknown = set(payload) - set(props)
    if unknown:
        violations.append(f"unexpected payload fields: {sorted(unknown)}")

    return violations


# ---------------------------------------------------------------------------
# Write execution — routes each intent to the correct SoR endpoint
# ---------------------------------------------------------------------------

async def _execute_write(
    intent: str,
    customer_id: str | None,
    payload: dict,
) -> dict:
    """
    Forward an intent to the appropriate SoR endpoint and return the raw
    SoR response body. Raises HTTPException if the SoR returns an error.
    """
    billing   = _http["billing"]
    catalogue = _http["catalogue"]

    if intent == "add_subscription":
        if not customer_id:
            raise HTTPException(status_code=422, detail="customer_id is required for add_subscription")
        resp = await billing.patch(
            f"/customers/{customer_id}",
            json={"add": {
                "sku":                  payload["sku"],
                "contract_term_months": payload.get("contract_term_months", 1),
                "stat":                 payload.get("stat", "A"),
            }},
        )

    elif intent == "cancel_subscription":
        if not customer_id:
            raise HTTPException(status_code=422, detail="customer_id is required for cancel_subscription")
        resp = await billing.patch(
            f"/customers/{customer_id}",
            json={"update": {
                "product_sku":   payload["product_sku"],
                "new_stat":      "C",
                "reason_code":   payload["reason_code"],
                "reason_detail": payload.get("reason_detail"),
            }},
        )

    elif intent == "update_product_price":
        resp = await catalogue.patch(
            f"/products/{payload['sku']}",
            json={"new_list_price_gbp": payload["new_list_price_gbp"]},
        )

    elif intent == "recalculate_billing":
        resp = await billing.post(
            "/recalculate",
            json={"product_sku": payload["product_sku"]},
        )

    else:
        # Should never reach here — intent was validated against WRITE_ROUTES
        raise HTTPException(status_code=500, detail=f"No executor for intent '{intent}'")

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"SoR returned {resp.status_code}: {resp.text[:300]}",
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _write_audit(
    audit_id: str,
    caller_id: str,
    intent: str,
    customer_id: str | None,
    payload: dict,
    outcome: str,           # "permitted" | "denied" | "error"
    denial_reason: str | None,
    sor_result: dict | None,
) -> dict:
    entry: dict[str, Any] = {
        "audit_id":      audit_id,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
        "caller_id":     caller_id,
        "intent":        intent,
        "customer_id":   customer_id,
        "payload":       payload,
        "outcome":       outcome,
        "denial_reason": denial_reason,
        "sor_result":    sor_result,
    }
    _audit_log.append(entry)
    return entry


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TokenRequest(BaseModel):
    """Token issuance request. client_id must be a registered demo client."""
    client_id:  str
    expires_in: int = 3600


class IntentRequest(BaseModel):
    """
    A write intent submitted by an agent caller.

    caller_id is derived from the verified JWT sub claim — it is NOT accepted
    from the request body. intent names the operation; customer_id scopes
    customer-facing writes; payload carries operation-specific fields.
    """
    intent:      str
    customer_id: str | None = None
    payload:     dict


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _http["billing"]   = httpx.AsyncClient(base_url=BILLING_URL,   timeout=10.0)
    _http["catalogue"] = httpx.AsyncClient(base_url=CATALOGUE_URL, timeout=10.0)
    _http["log"]       = httpx.AsyncClient(base_url=LOG_URL,        timeout=3.0)
    yield
    for client in _http.values():
        await client.aclose()
    _http.clear()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Action Broker",
    description=(
        "Write governance layer between the Agentic Layer and the Systems of Record. "
        "All agent write intents are resolved, permission-checked, schema-validated, "
        "and audited here before being forwarded to the target SoR. "
        "All routes except /token and /health require a Bearer JWT."
    ),
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/token", summary="Issue a JWT for a registered demo client")
async def issue_token(body: TokenRequest) -> dict:
    """
    Simplified client_credentials-style token endpoint.
    A real implementation would require a client_secret and use an
    authorisation server; this demo accepts client_id alone.

    Returns an OAuth2-compatible token response.
    """
    try:
        token = auth.mint_token(body.client_id, expires_in=body.expires_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error":              "invalid_client",
                "error_description":  str(exc),
                "registered_clients": list(auth.DEMO_CLIENTS),
            },
        )
    return {
        "access_token": token,
        "token_type":   "bearer",
        "expires_in":   body.expires_in,
        "client_id":    body.client_id,
    }


@app.post("/submit-intent", summary="Submit a governed write intent")
async def submit_intent(
    body:  IntentRequest,
    token: dict = Depends(_require_jwt),
) -> dict:
    """
    Execute a write intent through the full governance chain:
    JWT verification → permission check → route resolution → payload validation
    → SoR write → audit record → response.

    caller_id is derived from the JWT sub claim — it cannot be self-asserted.
    Returns the SoR response body enriched with audit_id and governance metadata.
    Permission failures return 403; schema violations return 422;
    SoR errors are proxied as-is.
    """
    # caller_id is authoritative from the verified token, never from the request body
    caller_id = token["sub"]
    audit_id  = str(uuid.uuid4())

    # ── 1. Permission check ─────────────────────────────────────────────────
    perm: AgentPermission | None = AGENT_PERMISSIONS.get(caller_id)
    if perm is None:
        _write_audit(
            audit_id, caller_id, body.intent, body.customer_id, body.payload,
            outcome="denied",
            denial_reason=f"caller '{caller_id}' has no write permissions registered",
            sor_result=None,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error":    "permission_denied",
                "message":  f"caller '{caller_id}' has no write permissions — read-only access only",
                "audit_id": audit_id,
            },
        )

    if body.intent not in perm.allowed_intents:
        denial = (
            f"caller '{caller_id}' is not permitted to submit intent "
            f"'{body.intent}' — allowed: {perm.allowed_intents}"
        )
        _write_audit(
            audit_id, caller_id, body.intent, body.customer_id, body.payload,
            outcome="denied",
            denial_reason=denial,
            sor_result=None,
        )
        try:
            await _http["log"].post("/logs", json={
                "service":   "action-broker",
                "action":    "intent_denied",
                "reason":    denial,
                "caller_id": caller_id,
                "intent":    body.intent,
                "outcome":   "denied",
                "details":   {"audit_id": audit_id},
            })
        except Exception:
            pass
        raise HTTPException(
            status_code=403,
            detail={
                "error":           "permission_denied",
                "message":         denial,
                "caller_id":       caller_id,
                "intent":          body.intent,
                "allowed_intents": perm.allowed_intents,
                "audit_id":        audit_id,
            },
        )

    # ── 2. Route resolution ─────────────────────────────────────────────────
    route: WriteRoute | None = WRITE_ROUTES.get(body.intent)
    if route is None:
        raise HTTPException(
            status_code=422,
            detail=f"intent '{body.intent}' has no declared write route",
        )

    # ── 3. Payload schema validation ────────────────────────────────────────
    violations = _validate_payload(body.payload, route.payload_schema)
    if violations:
        raise HTTPException(
            status_code=422,
            detail={
                "error":      "payload_validation_failed",
                "intent":     body.intent,
                "violations": violations,
            },
        )

    # ── 4. SoR write ────────────────────────────────────────────────────────
    try:
        sor_result = await _execute_write(body.intent, body.customer_id, body.payload)
    except HTTPException:
        _write_audit(
            audit_id, caller_id, body.intent, body.customer_id, body.payload,
            outcome="error", denial_reason=None, sor_result=None,
        )
        raise
    except Exception as exc:
        _write_audit(
            audit_id, caller_id, body.intent, body.customer_id, body.payload,
            outcome="error", denial_reason=str(exc), sor_result=None,
        )
        raise HTTPException(status_code=502, detail=f"SoR call failed: {exc}") from exc

    # ── 5. Audit record ─────────────────────────────────────────────────────
    _write_audit(
        audit_id, caller_id, body.intent, body.customer_id, body.payload,
        outcome="permitted", denial_reason=None, sor_result=sor_result,
    )

    try:
        await _http["log"].post("/logs", json={
            "service":     "action-broker",
            "action":      "intent_executed",
            "reason":      (
                f"caller '{caller_id}' executed intent '{body.intent}'"
                + (f" for customer {body.customer_id}" if body.customer_id else "")
            ),
            "caller_id":   caller_id,
            "intent":      body.intent,
            "customer_id": body.customer_id,
            "outcome":     "success",
            "details":     {"audit_id": audit_id, "target_sor": route.target_sor},
        })
    except Exception:
        pass

    # ── 6. Response ─────────────────────────────────────────────────────────
    return {
        "audit_id":    audit_id,
        "caller_id":   caller_id,
        "intent":      body.intent,
        "customer_id": body.customer_id,
        "target_sor":  route.target_sor,
        "outcome":     "permitted",
        **sor_result,
    }


@app.get("/permissions/{caller_id}", summary="Introspect what a caller is permitted to do")
async def get_permissions(
    caller_id: str,
    _token: dict = Depends(_require_jwt),
) -> dict:
    perm = AGENT_PERMISSIONS.get(caller_id)
    if not perm:
        raise HTTPException(status_code=404, detail=f"caller_id '{caller_id}' not registered")
    return {
        "caller_id":       perm.caller_id,
        "description":     perm.description,
        "allowed_intents": perm.allowed_intents,
        "write_routes": {
            intent: {
                "target_sor":  WRITE_ROUTES[intent].target_sor,
                "description": WRITE_ROUTES[intent].description,
            }
            for intent in perm.allowed_intents
            if intent in WRITE_ROUTES
        },
    }


@app.get("/permissions", summary="All declared caller permissions")
async def list_permissions(_token: dict = Depends(_require_jwt)) -> dict:
    return ontology.describe_agent_permissions()


@app.get("/write-routes", summary="All declared write routes")
async def list_write_routes(_token: dict = Depends(_require_jwt)) -> dict:
    return ontology.describe_write_routes()


@app.get("/audit", summary="Recent audit log (last 200 entries)")
async def get_audit(
    limit: int = 50,
    _token: dict = Depends(_require_jwt),
) -> list[dict]:
    entries = list(_audit_log)
    return entries[-min(limit, len(entries)):]


@app.get("/health", summary="Service health")
async def health() -> dict:
    return {
        "status":        "ok",
        "audit_entries": len(_audit_log),
        "callers":       list(AGENT_PERMISSIONS),
        "intents":       list(WRITE_ROUTES),
    }


if __name__ == "__main__":
    uvicorn.run("action_broker_app:app", host="0.0.0.0", port=8018, reload=True)
