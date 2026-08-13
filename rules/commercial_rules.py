"""
Commercial Rules Evaluation Engine — Business Context Layer
===========================================================
Evaluates discount policies against a customer context packet.
Pure Python — no service dependencies, fully unit-testable in isolation.

Discount policies are declared in rules/discounts.yaml. Each policy declares
a rule block that drives evaluation entirely from config — the engine contains
no knowledge of specific rule types. New rule behaviours are added by
introducing a new scope handler; existing policies are unaffected.

Two rule scopes are currently supported:

  per_product   — evaluates each active product individually against the rule's
                  match_field and threshold. Discount applies to each qualifying
                  product line.

  portfolio     — evaluates an aggregate (sum, avg, count) across all active
                  products against the rule's threshold. Discount applies to
                  every active product when the aggregate condition is met.

Policies are additive: if multiple rules fire, their discounts stack.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

_DISCOUNTS_FILE = Path(__file__).parent / "discounts.yaml"

_OPERATORS = {
    "gt":  lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt":  lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq":  lambda a, b: a == b,
}


@dataclass(frozen=True)
class DiscountPolicy:
    policy_id: str
    description: str
    discount_pct: float
    reason: str
    rule: MappingProxyType

    def __post_init__(self) -> None:
        # Wrap plain dicts so rule is truly immutable alongside frozen=True.
        if isinstance(self.rule, dict):
            object.__setattr__(self, "rule", MappingProxyType(self.rule))


@dataclass
class ProductDiscountResult:
    sku: str
    list_price_gbp: float
    applied_policies: list[str] = field(default_factory=list)
    total_discount_pct: float = 0.0
    discounted_price_gbp: float = 0.0


@dataclass
class DiscountSummary:
    customer_id: str | None
    policies_loaded: list[str]
    policies_applied: list[str]
    product_discounts: list[ProductDiscountResult]
    total_list_monthly_gbp: float
    total_discounted_monthly_gbp: float
    total_saving_gbp: float
    evaluated_at: str


# ---------------------------------------------------------------------------
# Policy registry — populated on first call or explicit reload
# ---------------------------------------------------------------------------

_policies: list[DiscountPolicy] = []


def reload_policies() -> list[str]:
    """Load discount policies from YAML. Returns the loaded policy IDs."""
    global _policies
    with open(_DISCOUNTS_FILE) as f:
        raw = yaml.safe_load(f)
    _policies = [
        DiscountPolicy(
            policy_id=entry["policy_id"],
            description=entry["description"],
            discount_pct=float(entry["discount_pct"]),
            reason=entry["reason"],
            rule=entry["rule"],
        )
        for entry in raw.get("policies", [])
    ]
    return [p.policy_id for p in _policies]


def _get_policies() -> list[DiscountPolicy]:
    if not _policies:
        reload_policies()
    return _policies


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _active_products(context: dict) -> list[dict]:
    """Extract active subscription/product lines from a context packet or commercial_state dict."""
    # Accept either a full context packet or just the commercial_state block.
    # Prefer "subscriptions" (post-billing-split canonical name); fall back to
    # "products" for backward compatibility with older fixtures and tests.
    commercial = context.get("commercial_state", context)
    lines = commercial.get("subscriptions", commercial.get("products", []))
    return [p for p in lines if p.get("status") == "active"]


def _apply_operator(value: Any, operator: str, threshold: Any) -> bool:
    op = _OPERATORS.get(operator)
    if op is None:
        raise ValueError(f"Unknown operator '{operator}' in discount rule")
    return op(value, threshold)


def _evaluate_policy(
    policy: DiscountPolicy,
    active: list[dict],
    product_policy_map: dict[str, list[str]],
    total_monthly: float,
) -> bool:
    """Evaluate one policy against the active product set. Returns True if the policy fired."""
    rule = policy.rule
    scope = rule.get("scope")
    operator = rule.get("operator", "gte")
    threshold = float(rule.get("threshold", 0))

    if scope == "per_product":
        match_field = rule["match_field"]
        matched = False
        for p in active:
            if _apply_operator(float(p.get(match_field, 0)), operator, threshold):
                sku = p.get("product_id", "")
                if sku in product_policy_map:
                    product_policy_map[sku].append(policy.policy_id)
                    matched = True
        return matched

    if scope == "portfolio":
        aggregate = rule.get("aggregate", "sum")
        match_field = rule["match_field"]
        values = [float(p.get(match_field, 0)) for p in active]
        if aggregate == "sum":
            agg_value = sum(values)
        elif aggregate == "avg":
            agg_value = sum(values) / len(values) if values else 0.0
        elif aggregate == "count":
            agg_value = float(len(values))
        else:
            raise ValueError(f"Unknown aggregate '{aggregate}' in discount rule")
        if not _apply_operator(agg_value, operator, threshold):
            return False
        for sku in product_policy_map:
            product_policy_map[sku].append(policy.policy_id)
        return True

    if scope == "sku_combination":
        requires = rule.get("requires", [])
        applies_to = rule.get("applies_to", "matched")
        # Each requirement must be satisfied by at least one active product.
        # Accumulate the SKUs that satisfied any requirement for applies_to: matched.
        matched_skus: set[str] = set()
        for req in requires:
            satisfying = [
                p for p in active
                if _apply_operator(p.get(req["match_field"]), req["operator"], req["value"])
            ]
            if not satisfying:
                return False  # combination incomplete — policy does not fire
            for p in satisfying:
                sku = p.get("product_id", "")
                if sku in product_policy_map:
                    matched_skus.add(sku)
        target_skus = set(product_policy_map.keys()) if applies_to == "all" else matched_skus
        for sku in target_skus:
            if sku in product_policy_map:
                product_policy_map[sku].append(policy.policy_id)
        return True

    raise ValueError(f"Unknown rule scope '{scope}' in policy '{policy.policy_id}'")


def evaluate_discounts(
    context: dict,
    customer_id: str | None = None,
) -> DiscountSummary:
    """
    Evaluate all discount policies against a customer context packet.

    Accepts either a full ACG context packet (with a 'commercial_state' key)
    or a bare commercial_state dict. Suspended and cancelled products are
    excluded from both the evaluation and the total monthly charge calculation.
    """
    policies = _get_policies()
    active = _active_products(context)

    # Evaluate against catalogue list prices, not contracted prices, to avoid
    # circular dependency when the portfolio threshold discount itself affects
    # the contracted price. Falls back to monthly_charge_gbp for older context formats.
    total_list = sum(p.get("list_price_gbp", p.get("monthly_charge_gbp", 0.0)) for p in active)

    # Per-product accumulator: sku -> list of matching policy_ids
    product_policy_map: dict[str, list[str]] = {
        p["product_id"]: []
        for p in active
        if "product_id" in p
    }

    applied_policy_ids: list[str] = []

    for policy in policies:
        if _evaluate_policy(policy, active, product_policy_map, total_list):
            applied_policy_ids.append(policy.policy_id)

    # Build per-product results
    policy_lookup = {p.policy_id: p for p in policies}
    product_results: list[ProductDiscountResult] = []

    for prod in active:
        sku = prod.get("product_id", "")
        list_price = prod.get("list_price_gbp", prod.get("monthly_charge_gbp", 0.0))
        applied = product_policy_map.get(sku, [])
        total_discount_pct = sum(
            policy_lookup[pid].discount_pct
            for pid in applied
            if pid in policy_lookup
        )
        discounted = round(list_price * (1 - total_discount_pct / 100), 2)

        product_results.append(ProductDiscountResult(
            sku=sku,
            list_price_gbp=list_price,
            applied_policies=list(applied),  # copy — avoids aliasing the accumulator
            total_discount_pct=total_discount_pct,
            discounted_price_gbp=discounted,
        ))

    total_discounted = round(sum(r.discounted_price_gbp for r in product_results), 2)
    total_list_rounded = round(total_list, 2)

    return DiscountSummary(
        customer_id=customer_id,
        policies_loaded=[p.policy_id for p in policies],
        policies_applied=applied_policy_ids,
        product_discounts=product_results,
        total_list_monthly_gbp=total_list_rounded,
        total_discounted_monthly_gbp=total_discounted,
        total_saving_gbp=round(total_list_rounded - total_discounted, 2),
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )
