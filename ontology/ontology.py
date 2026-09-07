"""
Ontology & Entity Resolution — Business Context Layer
=====================================================
Declares the assembly specification: how events from each SoR are
mapped to canonical domain partials, which events require a join,
and how domain partials are merged into a complete canonical record.

Field groups declare the structural units of cache documents. Each group
carries a _meta block (assembled_at, source_system, ttl_seconds,
consistency_class) enabling per-group freshness evaluation at ACG read time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


STATUS_CODES: dict[str, str] = {"A": "active", "S": "suspended", "C": "cancelled"}

# Top-level scalar fields present in every cache record (not field groups)
_CANONICAL_CONTROL_FIELDS: frozenset[str] = frozenset({
    "customer_id", "assembled_at", "assembly_state", "missing_domains", "provenance",
})

# Finite set of valid assembly states — owned here so cache_app imports rather than duplicates
@dataclass
class FieldMapping:
    """Describes how one SoR field maps to one canonical field."""
    sor_field: str
    canonical_field: str
    description: str
    transform: Callable[[Any], Any] | None = None


@dataclass
class FieldGroup:
    """
    Declares a logical grouping of canonical fields in a cache document.
    Each group carries its own _meta block enabling per-group freshness
    evaluation at ACG read time. TTL is declared here so the ontology
    is the single source of freshness policy.
    """
    name: str
    ttl_seconds: int
    consistency_class: str  # "strong" | "eventual"
    source_domain: str      # which domain event populates this group


@dataclass
class EventType:
    """
    Defines how a specific event type is assembled.

    Non-atomic events (atomic=False) must be joined with companion events
    before a complete canonical record can be written to the cache.
    """
    name: str
    source_system: str
    domain: str
    field_group: str                                 # name of the FieldGroup this domain populates
    atomic: bool                                     # True = alone produces complete record
    join_with: list[str]                             # companion event type names required
    join_key_sor: str                                # field in raw record for correlation
    join_key_canonical: str                          # canonical name of that key
    timeout_seconds: int
    field_mappings: list[FieldMapping] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Field group declarations — define the structure of cache documents
# ---------------------------------------------------------------------------

FIELD_GROUPS: dict[str, FieldGroup] = {
    "profile": FieldGroup(
        name="profile",
        ttl_seconds=3600,            # identity data changes infrequently
        consistency_class="eventual",
        source_domain="customer",
    ),
    "commercial_state": FieldGroup(
        name="commercial_state",
        ttl_seconds=300,             # billing status changes frequently
        consistency_class="strong",
        source_domain="billing",
    ),
}


# ---------------------------------------------------------------------------
# CRM field mappings — customer identity fields only
# ---------------------------------------------------------------------------
_CRM_MAPPINGS: list[FieldMapping] = [
    FieldMapping("cust_ref", "customer_id", "SoR customer reference resolved to canonical customer ID"),
    FieldMapping("first_nm", "first_name",  "Abbreviated first-name field normalised to full attribute name"),
    FieldMapping("last_nm",  "last_name",   "Abbreviated last-name field normalised to full attribute name"),
    FieldMapping("email",    "email",       "Email address carried through unchanged"),
    FieldMapping("phone",    "phone",       "Phone number carried through unchanged"),
]

# ---------------------------------------------------------------------------
# Billing subscription line mappings — applied per item in sub_lines[]
# product_name, product_type, and list_price_gbp are intentionally absent
# here; they are joined from the Product Catalogue by CDC at assembly time
# and stored inside commercial_state.subscriptions in the cache.
# ---------------------------------------------------------------------------
_BILLING_LINE_MAPPINGS: list[FieldMapping] = [
    FieldMapping("sku",                  "product_id",           "Stock-keeping unit mapped to canonical product identifier"),
    FieldMapping(
        "stat", "status",
        "Single-char status code (A/S/C) decoded to canonical status value",
        transform=lambda v: STATUS_CODES.get(v, v),
    ),
    FieldMapping("eff_from",             "effective_date",       "Effective-from date mapped to canonical effective_date"),
    FieldMapping("list_price_gbp",       "list_price_gbp",       "Catalogue list price in GBP at time of last billing event; base price for discount rule evaluation"),
    FieldMapping("monthly_charge",       "monthly_charge_gbp",   "Contracted price in GBP paid by the customer after all applicable discounts"),
    FieldMapping("total_discount_pct",   "total_discount_pct",   "Combined discount percentage applied across all fired policies"),
    FieldMapping("applied_discounts",    "applied_discounts",    "List of discount policies that fired, each with policy_id, discount_pct, and reason"),
    FieldMapping("discount_code",        "discount_code",        "Discount code applied at point of sale, carried through unchanged"),
    FieldMapping("contract_term_months", "contract_term_months", "Contract duration in months at purchase time (1 = monthly rolling, 24 = two-year)"),
    FieldMapping("reason_code",   "reason_code",   "Enumerated reason for the last status change (non_payment, customer_request, fraud_hold, technical_issue, network_fault, other)"),
    FieldMapping("reason_detail", "reason_detail", "Optional free-text context supplied by the agent or operator at the time of the status change"),
]


# ---------------------------------------------------------------------------
# Product Catalogue field mappings — applied by assemble_product_update()
# ---------------------------------------------------------------------------
_CATALOGUE_MAPPINGS: list[FieldMapping] = [
    FieldMapping("sku",            "product_id",    "SKU mapped to canonical product identifier"),
    FieldMapping("name",           "product_name",  "Product display name carried through unchanged"),
    FieldMapping("list_price_gbp", "list_price_gbp","List price in GBP carried through unchanged"),
    FieldMapping("product_type",   "product_type",  "Product type category carried through unchanged"),
    FieldMapping("available_terms_months", "available_terms_months",
                 "Available contract term durations in months carried through unchanged"),
]

PRODUCT_EVENT_TYPE = "product.catalogue.updated"


# ---------------------------------------------------------------------------
# Assembly specification — module-level registry of all event types
# ---------------------------------------------------------------------------
ASSEMBLY_SPEC: dict[str, EventType] = {
    "crm.customer.updated": EventType(
        name="crm.customer.updated",
        source_system="crm-sor",
        domain="customer",
        field_group="profile",
        atomic=False,
        join_with=["billing.subscription.updated"],
        join_key_sor="cust_ref",
        join_key_canonical="customer_id",
        timeout_seconds=30,
        field_mappings=_CRM_MAPPINGS,
    ),
    "billing.subscription.updated": EventType(
        name="billing.subscription.updated",
        source_system="billing-sor",
        domain="billing",
        field_group="commercial_state",
        atomic=False,
        join_with=["crm.customer.updated"],
        join_key_sor="cust_ref",
        join_key_canonical="customer_id",
        timeout_seconds=30,
        field_mappings=[],  # subscription lines handled specially in assemble_domain
    ),
}

REQUIRED_DOMAINS: frozenset[str] = frozenset(spec.domain for spec in ASSEMBLY_SPEC.values())

# Derived from REQUIRED_DOMAINS so adding a new EventType to ASSEMBLY_SPEC automatically
# registers its awaiting_<domain> state without touching this file again.
VALID_ASSEMBLY_STATES: frozenset[str] = (
    frozenset({"complete", "partial_timed_out"})
    | frozenset(f"awaiting_{d}" for d in REQUIRED_DOMAINS)
)


# ---------------------------------------------------------------------------
# Core assembly functions
# ---------------------------------------------------------------------------
def assemble_domain(raw: dict, event_type_name: str, event_id: str | None = None) -> dict:
    """
    Apply field mappings for one domain; returns a partial canonical dict.

    The partial contains customer_id at top level, a field group containing
    the domain's canonical fields with a _meta block, and provenance metadata.
    It is NOT a complete canonical record — merge_domains() completes it.
    """
    if event_id is None:
        event_id = str(uuid.uuid4())

    spec = ASSEMBLY_SPEC[event_type_name]
    fg = FIELD_GROUPS[spec.field_group]
    applied: list[str] = []
    group_fields: dict[str, Any] = {}

    if spec.domain == "customer":
        for m in spec.field_mappings:
            if m.sor_field in raw:
                val = m.transform(raw[m.sor_field]) if m.transform else raw[m.sor_field]
                group_fields[m.canonical_field] = val
                applied.append(f"{m.sor_field} → {m.canonical_field}")
        group_fields["full_name"] = (
            f"{group_fields.get('first_name', '')} {group_fields.get('last_name', '')}".strip()
        )
        applied.append("first_nm + last_nm → full_name  [derived]")

    elif spec.domain == "billing":
        subscriptions: list[dict] = []
        for raw_sub in raw.get("sub_lines", []):
            canonical_sub: dict[str, Any] = {}
            for m in _BILLING_LINE_MAPPINGS:
                if m.sor_field in raw_sub:
                    val = m.transform(raw_sub[m.sor_field]) if m.transform else raw_sub[m.sor_field]
                    canonical_sub[m.canonical_field] = val
            subscriptions.append(canonical_sub)
        group_fields["subscriptions"] = subscriptions
        applied.append(
            f"sub_lines[{len(subscriptions)}] → subscriptions[]  "
            f"[ontology-mapped, {len(_BILLING_LINE_MAPPINGS)} field mappings per subscription]"
        )

    assembled_at = datetime.now(timezone.utc).isoformat()

    # _meta carries freshness policy alongside the assembled timestamp so the ACG
    # can evaluate freshness at read time without querying the ontology separately
    group_fields["_meta"] = {
        "assembled_at":      assembled_at,
        "source_system":     spec.source_system,
        "ttl_seconds":       fg.ttl_seconds,
        "consistency_class": fg.consistency_class,
    }

    partial: dict[str, Any] = {}

    # customer_id at top level so CDC can correlate events across domains
    if spec.join_key_sor in raw:
        partial[spec.join_key_canonical] = raw[spec.join_key_sor]

    partial[spec.field_group] = group_fields
    partial["assembled_at"] = assembled_at
    partial["provenance"] = {
        "domain":           spec.domain,
        "source_system":    spec.source_system,
        "event_id":         event_id,
        "applied_mappings": applied,
        "mapping_count":    len(applied),
    }

    return partial


def assemble_product_update(raw: dict) -> dict:
    """
    Apply catalogue field mappings to a raw product record from the Catalogue SoR.
    Returns a canonical product dict for use by CDC when handling
    product.catalogue.updated events.
    """
    canonical: dict[str, Any] = {}
    applied: list[str] = []
    for m in _CATALOGUE_MAPPINGS:
        if m.sor_field in raw:
            val = m.transform(raw[m.sor_field]) if m.transform else raw[m.sor_field]
            canonical[m.canonical_field] = val
            applied.append(f"{m.sor_field} → {m.canonical_field}")
    canonical["provenance"] = {
        "source_system":    "product-catalogue",
        "applied_mappings": applied,
        "mapping_count":    len(applied),
    }
    return canonical


def merge_domains(domains: dict[str, dict]) -> dict:
    """
    Merge two domain partials into one canonical record.

    Each domain contributes its field group key to the merged record.
    Top-level scalar fields (customer_id) are included directly.
    Provenance is preserved per-domain so each field's origin remains traceable.
    """
    merged: dict[str, Any] = {}
    domain_provenance: dict[str, Any] = {}
    source_systems: list[str] = []

    for domain_name, partial in domains.items():
        prov = partial.get("provenance", {})
        domain_provenance[domain_name] = prov
        ss = prov.get("source_system")
        if ss and ss not in source_systems:
            source_systems.append(ss)

        for k, v in partial.items():
            if k not in ("provenance", "assembled_at"):
                merged[k] = v

    # Record-level assembled_at is the latest across all domain partials
    assembled_ats = [p.get("assembled_at") for p in domains.values() if p.get("assembled_at")]
    merged["assembled_at"] = max(assembled_ats) if assembled_ats else datetime.now(timezone.utc).isoformat()

    merged["provenance"] = {
        "source_systems": source_systems,
        "domains": domain_provenance,
    }

    return merged


# ---------------------------------------------------------------------------
# Schema validation — used by cache_app to gate PUT /records/{id}
# ---------------------------------------------------------------------------

def _field_group_allowed_keys() -> dict[str, frozenset[str]]:
    """
    Derive the allowed canonical field names for each field group from ASSEMBLY_SPEC.
    This keeps schema knowledge in the ontology — cache_app just calls validate_cache_record().
    """
    result: dict[str, frozenset[str]] = {}
    for spec in ASSEMBLY_SPEC.values():
        fg = spec.field_group
        if spec.domain == "customer":
            # Canonical fields from CRM mappings plus the derived composite
            keys: frozenset[str] = (
                frozenset(m.canonical_field for m in spec.field_mappings)
                | {"full_name", "_meta"}
            )
        elif spec.domain == "billing":
            # Billing domain assembles to a `subscriptions` list. CDC also writes
            # `discount_summary` into this group after enriching from the Catalogue
            # and evaluating discount policies via the Offer Engine at assembly time.
            keys = frozenset({"subscriptions", "discount_summary", "_meta"})
        else:
            keys = frozenset({"_meta"})
        result[fg] = keys
    return result


def validate_cache_record(body: dict) -> list[str]:
    """
    Validate a candidate cache record body against the ontology-declared schema.

    Returns a list of violation strings; an empty list means the body is valid.
    Partial records (with only some field groups present) are valid — CDC writes
    them during stateful joins while waiting for companion domain events.
    """
    violations: list[str] = []
    allowed_top = _CANONICAL_CONTROL_FIELDS | frozenset(FIELD_GROUPS)
    fg_allowed = _field_group_allowed_keys()

    # Reject any top-level key not declared in the ontology
    unknown_top = set(body) - allowed_top
    if unknown_top:
        violations.append(f"Unknown top-level keys: {sorted(unknown_top)}")

    # Validate each field group that is present in the body
    for fg_name in FIELD_GROUPS:
        fg_value = body.get(fg_name)
        if fg_value is None:
            continue  # absent group is fine — partial records are valid
        if not isinstance(fg_value, dict):
            violations.append(
                f"Field group '{fg_name}' must be a dict, got {type(fg_value).__name__}"
            )
            continue
        if "_meta" not in fg_value:
            violations.append(f"Field group '{fg_name}' missing required '_meta' block")
        unknown_fg = set(fg_value) - fg_allowed.get(fg_name, frozenset())
        if unknown_fg:
            violations.append(
                f"Unknown keys in field group '{fg_name}': {sorted(unknown_fg)}"
            )

    # assembly_state, when present, must be a declared value
    state = body.get("assembly_state")
    if state is not None and state not in VALID_ASSEMBLY_STATES:
        violations.append(
            f"Invalid assembly_state '{state}'; allowed: {sorted(VALID_ASSEMBLY_STATES)}"
        )

    return violations


# ---------------------------------------------------------------------------
# Introspection — used by ACG /ontology endpoint
# ---------------------------------------------------------------------------
def describe_spec() -> dict:
    """Return machine-readable assembly specification including field group declarations."""
    groups = {
        name: {
            "ttl_seconds":       fg.ttl_seconds,
            "consistency_class": fg.consistency_class,
            "source_domain":     fg.source_domain,
        }
        for name, fg in FIELD_GROUPS.items()
    }

    events: dict[str, Any] = {}
    for name, spec in ASSEMBLY_SPEC.items():
        if spec.domain == "billing":
            field_maps = [
                {
                    "sor_field":       m.sor_field,
                    "canonical_field": m.canonical_field,
                    "description":     m.description,
                    "has_transform":   m.transform is not None,
                }
                for m in _BILLING_LINE_MAPPINGS
            ]
        else:
            field_maps = [
                {
                    "sor_field":       m.sor_field,
                    "canonical_field": m.canonical_field,
                    "description":     m.description,
                    "has_transform":   m.transform is not None,
                }
                for m in spec.field_mappings
            ]
        events[name] = {
            "source_system":      spec.source_system,
            "domain":             spec.domain,
            "field_group":        spec.field_group,
            "atomic":             spec.atomic,
            "join_with":          spec.join_with,
            "join_key_sor":       spec.join_key_sor,
            "join_key_canonical": spec.join_key_canonical,
            "timeout_seconds":    spec.timeout_seconds,
            "field_mappings":     field_maps,
        }

    fg_allowed = _field_group_allowed_keys()
    cache_schema = {
        "control_fields":       sorted(_CANONICAL_CONTROL_FIELDS),
        "valid_assembly_states": sorted(VALID_ASSEMBLY_STATES),
        "field_group_keys": {
            fg: sorted(keys) for fg, keys in fg_allowed.items()
        },
    }

    catalogue_maps = [
        {
            "sor_field":       m.sor_field,
            "canonical_field": m.canonical_field,
            "description":     m.description,
            "has_transform":   m.transform is not None,
        }
        for m in _CATALOGUE_MAPPINGS
    ]

    return {
        "field_groups":             groups,
        "events":                   events,
        "cache_schema":             cache_schema,
        "catalogue_field_mappings": catalogue_maps,
        "product_event_type":       PRODUCT_EVENT_TYPE,
        "retrieval_plans":          describe_retrieval_plans(),
        "response_contracts":       {pid: describe_response_contract(pid) for pid in RETRIEVAL_PLANS},
    }


# ---------------------------------------------------------------------------
# Write route and permission declarations — Action Broker governance substrate
# ---------------------------------------------------------------------------

@dataclass
class WriteRoute:
    """
    Maps a named write intent to the target SoR endpoint and a minimal schema
    describing the required payload fields. The Action Broker resolves these
    at runtime; callers submit intents by name, never SoR URLs directly.
    """
    intent: str
    description: str
    target_sor: str      # "billing-sor" | "catalogue-sor"
    # Minimal schema: {"required": [...], "properties": {field: {"type": ...}}}
    payload_schema: dict


@dataclass
class AgentPermission:
    """
    Declares what write intents a named caller is permitted to submit.
    Read access to ACG endpoints is assumed for all callers; only write
    intents are governed here.
    """
    caller_id: str
    description: str
    allowed_intents: list[str]


WRITE_ROUTES: dict[str, WriteRoute] = {
    "add_subscription": WriteRoute(
        intent="add_subscription",
        description=(
            "Add a new subscription line to a customer's Billing SoR record. "
            "Price is always sourced from the Product Catalogue — callers supply "
            "the SKU and contract term only."
        ),
        target_sor="billing-sor",
        payload_schema={
            "required": ["sku"],
            "properties": {
                "sku":                  {"type": str},
                "contract_term_months": {"type": int},
                "stat":                 {"type": str},
            },
        },
    ),
    "cancel_subscription": WriteRoute(
        intent="cancel_subscription",
        description=(
            "Cancel an existing active subscription line in the Billing SoR. "
            "reason_code is required; reason_detail is optional free text."
        ),
        target_sor="billing-sor",
        payload_schema={
            "required": ["product_sku", "reason_code"],
            "properties": {
                "product_sku":   {"type": str},
                "reason_code":   {"type": str},
                "reason_detail": {"type": str},
            },
        },
    ),
    "update_product_price": WriteRoute(
        intent="update_product_price",
        description=(
            "Update the list price of a product in the Product Catalogue SoR. "
            "Triggers a product.catalogue.updated CDC fan-out that re-enriches "
            "cached product metadata for all holders. Does NOT update contracted "
            "billing charges — that requires a separate recalculate_billing intent."
        ),
        target_sor="catalogue-sor",
        payload_schema={
            "required": ["sku", "new_list_price_gbp"],
            "properties": {
                "sku":                {"type": str},
                "new_list_price_gbp": {"type": (int, float)},
            },
        },
    ),
    "recalculate_billing": WriteRoute(
        intent="recalculate_billing",
        description=(
            "Trigger a bulk billing recalculation for all customers holding a given "
            "SKU. Fetches the current list price from the Product Catalogue, "
            "re-evaluates discount rules for each holder's full active portfolio, "
            "and emits billing.subscription.updated events to CDC so the cache and "
            "permanent record are refreshed."
        ),
        target_sor="billing-sor",
        payload_schema={
            "required": ["product_sku"],
            "properties": {
                "product_sku": {"type": str},
            },
        },
    ),
}


AGENT_PERMISSIONS: dict[str, AgentPermission] = {
    "purchase-agent": AgentPermission(
        caller_id="purchase-agent",
        description=(
            "Customer-facing purchase agent. May add new subscriptions and cancel "
            "existing ones in the Billing SoR. Cannot modify product prices or "
            "trigger billing recalculations."
        ),
        allowed_intents=["add_subscription", "cancel_subscription"],
    ),
    "catalogue-admin": AgentPermission(
        caller_id="catalogue-admin",
        description=(
            "Product catalogue administrator. May update product list prices in the "
            "Catalogue SoR and trigger bulk billing recalculations. Cannot directly "
            "modify customer subscription records."
        ),
        allowed_intents=["update_product_price", "recalculate_billing"],
    ),
    "system-admin": AgentPermission(
        caller_id="system-admin",
        description=(
            "System administrator with full write access across all SoRs. "
            "Intended for operational tasks, demo teardown, and break-glass scenarios."
        ),
        allowed_intents=[
            "add_subscription",
            "cancel_subscription",
            "update_product_price",
            "recalculate_billing",
        ],
    ),
}


# ---------------------------------------------------------------------------
# Retrieval plan declarations — ACG read strategy, owned by the ontology
# ---------------------------------------------------------------------------

@dataclass
class RetrievalStore:
    """
    One store consulted in a retrieval plan.

    role="cache_tier": consulted in sequence; on a miss the ACG moves to the
    store named by on_miss (or returns 404 if on_miss is None).
    role="enrichment": always called after the cache tier chain resolves
    successfully; on_miss is not used.

    output_fields: JSON Schema-style dict declaring the fields this store adds
    to the ACG response. Absent for cache_tier stores — their output is fully
    described by FIELD_GROUPS and _CANONICAL_CONTROL_FIELDS. Required for
    enrichment stores so the full response contract is ontology-declared.
    """
    name: str
    description: str
    role: str               # "cache_tier" | "enrichment"
    timeout_seconds: float
    on_miss: str | None = None          # next store to try on 404 (cache_tier only)
    output_fields: dict | None = None   # enrichment output schema (enrichment role only)


@dataclass
class RetrievalPlan:
    """
    Declares a named ACG retrieval strategy: which stores to consult, in what
    order, and how to handle misses and enrichment.

    The ACG reads this declaration to drive execution. The pydantic-graph nodes
    in acg_app.py implement it; this dataclass is the authoritative specification.
    """
    plan_id: str
    description: str
    task_type: str          # "customer_context" | "compatible_offers"
    entry_store: str        # name of the first RetrievalStore to query
    stores: dict[str, "RetrievalStore"] = field(default_factory=dict)


RETRIEVAL_PLANS: dict[str, RetrievalPlan] = {
    "customer-billing-context-v4": RetrievalPlan(
        plan_id="customer-billing-context-v4",
        description=(
            "Two-tier cache-first retrieval of fully-assembled canonical customer context. "
            "Reads the hot cache (Tier 2/3) first; on a miss falls back to the permanent store (Tier 1). "
            "Known customers are always present in the permanent store — populated at startup by the SoRs "
            "emitting events through the CDC assembly pipeline. "
            "A permanent-store miss means the customer is genuinely unknown: the ACG logs it and returns 404. "
            "The ACG is a pure reader — it never drives assembly or publishes events. "
            "Partial records (assembly_state != complete) are returned transparently with missing_domains."
        ),
        task_type="customer_context",
        entry_store="hot_cache",
        stores={
            "hot_cache": RetrievalStore(
                name="hot_cache",
                description="In-memory hot cache (Tier 2/3). Populated by CDC on every assembly event.",
                role="cache_tier",
                timeout_seconds=5.0,
                on_miss="permanent_store",
            ),
            "permanent_store": RetrievalStore(
                name="permanent_store",
                description=(
                    "Permanent customer index (Tier 1). Always populated for known customers. "
                    "A miss here means the customer is genuinely unknown."
                ),
                role="cache_tier",
                timeout_seconds=5.0,
                on_miss=None,
            ),
        },
    ),
    "compatible-offers-v1": RetrievalPlan(
        plan_id="compatible-offers-v1",
        description=(
            "Two-tier cache-first retrieval of compatible product offers for a customer. "
            "Reads the assembled customer context from the hot cache (Tier 2/3), falling back to the "
            "permanent store (Tier 1) on a miss. Passes the customer's commercial_state to the Offer "
            "Engine, which walks the product graph from each held SKU to find structurally compatible "
            "and upgrade-path products not yet held, then evaluates the discount policy delta for each "
            "available contract term. Returns compatible products with per-term savings proposals."
        ),
        task_type="compatible_offers",
        entry_store="hot_cache",
        stores={
            "hot_cache": RetrievalStore(
                name="hot_cache",
                description="In-memory hot cache (Tier 2/3). Populated by CDC on every assembly event.",
                role="cache_tier",
                timeout_seconds=5.0,
                on_miss="permanent_store",
            ),
            "permanent_store": RetrievalStore(
                name="permanent_store",
                description=(
                    "Permanent customer index (Tier 1). Always populated for known customers. "
                    "A miss here means the customer is genuinely unknown."
                ),
                role="cache_tier",
                timeout_seconds=5.0,
                on_miss=None,
            ),
            "offer_engine": RetrievalStore(
                name="offer_engine",
                description=(
                    "Offer Engine enrichment. Receives the customer's commercial_state and "
                    "walks the product graph to find compatible products with per-term discount proposals."
                ),
                role="enrichment",
                timeout_seconds=10.0,
                on_miss=None,
                output_fields={
                    "held_skus": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Canonical product IDs of all active subscriptions currently held "
                            "by the customer, sorted alphabetically."
                        ),
                    },
                    "compatible_products": {
                        "type": "array",
                        "description": (
                            "Products structurally compatible with the customer's current holdings "
                            "that they do not already hold, each with per-term discount proposals. "
                            "Empty list if no compatible products exist."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "sku": {
                                    "type": "string",
                                    "description": "Canonical product identifier.",
                                },
                                "product_name": {
                                    "type": "string",
                                    "description": "Display name from the Product Catalogue.",
                                },
                                "product_type": {
                                    "type": "string",
                                    "description": "Product category (e.g. broadband, tv, mobile).",
                                },
                                "list_price_gbp": {
                                    "type": "number",
                                    "description": "Standard monthly list price in GBP before discounts.",
                                },
                                "available_terms_months": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": (
                                        "Available contract durations in months "
                                        "(e.g. [1, 12, 24]). One proposal is returned per term."
                                    ),
                                },
                                "compatible_via": {
                                    "type": "string",
                                    "enum": ["compatible", "upgrade"],
                                    "description": (
                                        "'compatible' — product can be held alongside current holdings. "
                                        "'upgrade' — product replaces a currently held product."
                                    ),
                                },
                                "proposals": {
                                    "type": "array",
                                    "description": "One discount proposal per available contract term.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "contract_term_months": {
                                                "type": "integer",
                                                "description": "Contract duration this proposal applies to.",
                                            },
                                            "monthly_charge_gbp": {
                                                "type": "number",
                                                "description": (
                                                    "List price for this product at this term. "
                                                    "The actual saving is in the delta block."
                                                ),
                                            },
                                            "delta": {
                                                "type": "object",
                                                "description": (
                                                    "Before/after comparison of the customer's total "
                                                    "monthly charge and discount position if this product "
                                                    "were added at this term."
                                                ),
                                                "properties": {
                                                    "monthly_charge_delta_gbp": {
                                                        "type": "number",
                                                        "description": (
                                                            "Change in total monthly charge across all "
                                                            "products. Negative = overall saving."
                                                        ),
                                                    },
                                                    "saving_delta_gbp": {
                                                        "type": "number",
                                                        "description": (
                                                            "Additional saving vs the customer's current "
                                                            "discount position."
                                                        ),
                                                    },
                                                    "new_policies_applied": {
                                                        "type": "array",
                                                        "items": {"type": "string"},
                                                        "description": (
                                                            "Discount policy IDs that would newly fire "
                                                            "if this product were added."
                                                        ),
                                                    },
                                                    "lost_policies": {
                                                        "type": "array",
                                                        "items": {"type": "string"},
                                                        "description": (
                                                            "Policy IDs no longer applying after this "
                                                            "addition (e.g. displaced by a better tier)."
                                                        ),
                                                    },
                                                    "net_change": {
                                                        "type": "string",
                                                        "enum": ["saving", "cost_increase", "neutral"],
                                                        "description": (
                                                            "Net direction of the total monthly charge "
                                                            "change. 'saving' means the customer pays less "
                                                            "overall due to discount uplift."
                                                        ),
                                                    },
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            ),
        },
    ),
}


# Human-readable descriptions for each field group — used in response contracts
# and MCP tool descriptions. Keyed on FIELD_GROUPS names.
_FIELD_GROUP_DESCRIPTIONS: dict[str, str] = {
    "profile":          "Customer identity fields assembled from the CRM SoR.",
    "commercial_state": "Billing subscription state assembled from the Billing SoR.",
}

# Consumer-facing descriptions for each valid assembly state.
# Must stay in sync with VALID_ASSEMBLY_STATES — validated by tests.
_ASSEMBLY_STATE_DESCRIPTIONS: dict[str, str] = {
    "complete": (
        "All domains assembled. All declared field groups are present and valid."
    ),
    "awaiting_billing": (
        "CRM identity assembled; billing event not yet received. "
        "commercial_state is absent. Check missing_domains."
    ),
    "awaiting_customer": (
        "Billing subscriptions assembled; CRM event not yet received. "
        "profile is absent. Check missing_domains."
    ),
    "partial_timed_out": (
        "Assembly join timed out before all domains arrived. "
        "Fields in present groups are valid. Check missing_domains for absent groups."
    ),
}

# Fields the ACG adds to every response that are not part of the cached record.
_ACG_ENVELOPE_FIELDS: dict[str, dict] = {
    "plan_id": {
        "type": "string",
        "description": "Identifier of the retrieval plan that produced this response.",
    },
    "cache_hit": {
        "type": "boolean",
        "description": (
            "True if the record was served from the hot cache (Tier 2/3); "
            "False if retrieved from the permanent store (Tier 1)."
        ),
    },
    "retrieval_source": {
        "type": "string",
        "enum": ["hot_cache", "permanent_store"],
        "description": "Which cache tier the record was read from.",
    },
}

# Descriptions for the scalar control fields present at the top level of every
# cached record. Keyed on _CANONICAL_CONTROL_FIELDS names, plus 'version' which
# is a cache implementation field not in the control set.
_CONTROL_FIELD_DESCRIPTIONS: dict[str, dict] = {
    "customer_id": {
        "type": "string",
        "description": "Canonical customer identifier.",
    },
    "assembled_at": {
        "type": "string",
        "description": "ISO-8601 timestamp of the most recent domain assembly across all field groups.",
    },
    "assembly_state": {
        "type": "string",
        "values": _ASSEMBLY_STATE_DESCRIPTIONS,
        "note": (
            "Always check assembly_state before acting on the response. "
            "Partial records are returned transparently."
        ),
    },
    "missing_domains": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Domain names absent from this record due to a pending join or timeout.",
    },
    "version": {
        "type": "integer",
        "description": "Monotonic version counter incremented on each cache write.",
    },
    "provenance": {
        "type": "object",
        "description": "Per-domain assembly provenance: source system, applied field mappings, event IDs.",
    },
}


def describe_response_contract(plan_id: str) -> dict:
    """
    Return the full response contract for a retrieval plan.

    Combines ACG envelope fields, cache record control fields, per-field-group
    schemas with freshness semantics, assembly state consumer descriptions,
    and enrichment store output fields (for plans with enrichment stores).

    Cache-tier output is derived from FIELD_GROUPS. Enrichment output is taken
    from each enrichment store's output_fields declaration.
    """
    plan = RETRIEVAL_PLANS[plan_id]
    fg_keys = _field_group_allowed_keys()

    field_groups = {}
    for fg_name, fg in FIELD_GROUPS.items():
        canonical_fields = sorted(
            k for k in fg_keys.get(fg_name, frozenset()) if k != "_meta"
        )
        field_groups[fg_name] = {
            "description":       _FIELD_GROUP_DESCRIPTIONS.get(fg_name, ""),
            "fields":            canonical_fields,
            "ttl_seconds":       fg.ttl_seconds,
            "consistency_class": fg.consistency_class,
            "meta_freshness": {
                "field":     "_meta.freshness",
                "confirmed": f"assembled_at is within {fg.ttl_seconds}s of now.",
                "stale":     f"assembled_at is older than {fg.ttl_seconds}s.",
                "unknown":   "No ttl metadata present in this record.",
            },
        }

    enrichment_outputs = {
        name: {"output_fields": store.output_fields}
        for name, store in plan.stores.items()
        if store.role == "enrichment" and store.output_fields
    }

    return {
        "envelope_fields":   _ACG_ENVELOPE_FIELDS,
        "control_fields":    _CONTROL_FIELD_DESCRIPTIONS,
        "field_groups":      field_groups,
        "assembly_states":   _ASSEMBLY_STATE_DESCRIPTIONS,
        "enrichment_outputs": enrichment_outputs,
    }


def describe_retrieval_plans() -> dict:
    """Return machine-readable retrieval plan declarations for ACG introspection."""
    return {
        plan_id: {
            "plan_id":     plan.plan_id,
            "description": plan.description,
            "task_type":   plan.task_type,
            "entry_store": plan.entry_store,
            "stores": {
                name: {
                    "description":     store.description,
                    "role":            store.role,
                    "timeout_seconds": store.timeout_seconds,
                    "on_miss":         store.on_miss,
                    "output_fields":   store.output_fields,
                }
                for name, store in plan.stores.items()
            },
        }
        for plan_id, plan in RETRIEVAL_PLANS.items()
    }


def describe_write_routes() -> dict:
    """Return machine-readable write route declarations for Action Broker introspection."""
    return {
        intent: {
            "description": route.description,
            "target_sor":  route.target_sor,
            "required_payload_fields": route.payload_schema.get("required", []),
            "payload_fields": {
                field: {"type": t.__name__ if isinstance(t, type) else str(t)}
                for field, spec in route.payload_schema.get("properties", {}).items()
                for t in [spec["type"]]
            },
        }
        for intent, route in WRITE_ROUTES.items()
    }


def describe_agent_permissions() -> dict:
    """Return machine-readable caller permission declarations."""
    return {
        caller_id: {
            "description":     perm.description,
            "allowed_intents": perm.allowed_intents,
        }
        for caller_id, perm in AGENT_PERMISSIONS.items()
    }
