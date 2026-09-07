"""
Unit tests for retrieval plan declarations in the ontology.
No services required — imports ontology directly.
"""
import json

import pytest
import sys
import os

# Ensure the ontology package is importable without running services
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ontology"))

import ontology


def test_expected_plans_are_declared():
    assert "customer-billing-context-v4" in ontology.RETRIEVAL_PLANS
    assert "compatible-offers-v1" in ontology.RETRIEVAL_PLANS


def test_each_plan_has_required_fields():
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        assert plan.plan_id == plan_id, f"plan_id mismatch in '{plan_id}'"
        assert plan.description, f"Plan '{plan_id}' has empty description"
        assert plan.task_type, f"Plan '{plan_id}' has empty task_type"
        assert plan.entry_store, f"Plan '{plan_id}' has empty entry_store"
        assert plan.stores, f"Plan '{plan_id}' has no stores"


def test_entry_store_exists_in_stores():
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        assert plan.entry_store in plan.stores, (
            f"Plan '{plan_id}': entry_store '{plan.entry_store}' is not declared in stores"
        )


def test_on_miss_references_valid_store():
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        for store_name, store in plan.stores.items():
            if store.on_miss is not None:
                assert store.on_miss in plan.stores, (
                    f"Plan '{plan_id}', store '{store_name}': "
                    f"on_miss '{store.on_miss}' is not a declared store"
                )


def test_cache_tier_chain_is_acyclic():
    """No on_miss chain should loop back to a previously visited store."""
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        for start in plan.stores:
            visited: set[str] = set()
            current: str | None = start
            while current is not None:
                assert current not in visited, (
                    f"Plan '{plan_id}': cycle detected in cache_tier chain at '{current}'"
                )
                visited.add(current)
                store = plan.stores[current]
                current = store.on_miss if store.role == "cache_tier" else None


def test_store_roles_are_valid():
    valid_roles = {"cache_tier", "enrichment"}
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        for store_name, store in plan.stores.items():
            assert store.role in valid_roles, (
                f"Plan '{plan_id}', store '{store_name}': "
                f"invalid role '{store.role}' — must be one of {valid_roles}"
            )


def test_enrichment_stores_have_no_on_miss():
    """Enrichment stores are always called; on_miss is not meaningful for them."""
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        for store_name, store in plan.stores.items():
            if store.role == "enrichment":
                assert store.on_miss is None, (
                    f"Plan '{plan_id}', store '{store_name}': "
                    f"enrichment store should not declare on_miss (got '{store.on_miss}')"
                )


def test_describe_retrieval_plans_is_json_serialisable():
    result = ontology.describe_retrieval_plans()
    # Should not raise
    serialised = json.dumps(result)
    assert len(serialised) > 0


def test_describe_retrieval_plans_structure():
    result = ontology.describe_retrieval_plans()
    for plan_id, plan_dict in result.items():
        assert "plan_id" in plan_dict
        assert "description" in plan_dict
        assert "task_type" in plan_dict
        assert "entry_store" in plan_dict
        assert "stores" in plan_dict
        for store_name, store_dict in plan_dict["stores"].items():
            assert "description" in store_dict
            assert "role" in store_dict
            assert "timeout_seconds" in store_dict
            assert "on_miss" in store_dict


def test_describe_spec_includes_retrieval_plans():
    spec = ontology.describe_spec()
    assert "retrieval_plans" in spec, "describe_spec() must include 'retrieval_plans'"
    plans = spec["retrieval_plans"]
    assert "customer-billing-context-v4" in plans
    assert "compatible-offers-v1" in plans


def test_customer_context_plan_has_two_cache_tiers():
    plan = ontology.RETRIEVAL_PLANS["customer-billing-context-v4"]
    cache_tiers = [s for s in plan.stores.values() if s.role == "cache_tier"]
    assert len(cache_tiers) == 2, "customer context plan must declare hot_cache and permanent_store"


def test_compatible_offers_plan_has_enrichment_store():
    plan = ontology.RETRIEVAL_PLANS["compatible-offers-v1"]
    enrichment = [s for s in plan.stores.values() if s.role == "enrichment"]
    assert len(enrichment) == 1
    assert enrichment[0].name == "offer_engine"


def test_offer_engine_store_declares_output_fields():
    plan = ontology.RETRIEVAL_PLANS["compatible-offers-v1"]
    offer_store = plan.stores["offer_engine"]
    assert offer_store.output_fields is not None, "offer_engine must declare output_fields"
    assert "held_skus" in offer_store.output_fields
    assert "compatible_products" in offer_store.output_fields


def test_offer_engine_output_fields_are_json_serialisable():
    plan = ontology.RETRIEVAL_PLANS["compatible-offers-v1"]
    offer_store = plan.stores["offer_engine"]
    serialised = json.dumps(offer_store.output_fields)
    assert len(serialised) > 0


def test_offer_engine_compatible_products_schema_has_required_keys():
    plan = ontology.RETRIEVAL_PLANS["compatible-offers-v1"]
    items_props = (
        plan.stores["offer_engine"]
        .output_fields["compatible_products"]["items"]["properties"]
    )
    for key in ("sku", "product_name", "list_price_gbp", "compatible_via", "proposals"):
        assert key in items_props, f"compatible_products items missing '{key}'"


def test_offer_engine_delta_schema_has_required_keys():
    plan = ontology.RETRIEVAL_PLANS["compatible-offers-v1"]
    delta_props = (
        plan.stores["offer_engine"]
        .output_fields["compatible_products"]["items"]["properties"]
        ["proposals"]["items"]["properties"]["delta"]["properties"]
    )
    for key in ("monthly_charge_delta_gbp", "saving_delta_gbp",
                "new_policies_applied", "lost_policies", "net_change"):
        assert key in delta_props, f"delta schema missing '{key}'"


def test_cache_tier_stores_have_no_output_fields():
    """Cache-tier output is described by FIELD_GROUPS; output_fields should be absent."""
    for plan_id, plan in ontology.RETRIEVAL_PLANS.items():
        for store_name, store in plan.stores.items():
            if store.role == "cache_tier":
                assert store.output_fields is None, (
                    f"Plan '{plan_id}', store '{store_name}': cache_tier stores must not "
                    f"declare output_fields (use FIELD_GROUPS instead)"
                )


def test_describe_retrieval_plans_includes_output_fields():
    result = ontology.describe_retrieval_plans()
    offer_store = result["compatible-offers-v1"]["stores"]["offer_engine"]
    assert "output_fields" in offer_store
    assert offer_store["output_fields"] is not None
    assert "held_skus" in offer_store["output_fields"]
    assert "compatible_products" in offer_store["output_fields"]


# ---------------------------------------------------------------------------
# describe_response_contract tests
# ---------------------------------------------------------------------------

def test_response_contract_has_required_top_level_keys():
    for plan_id in ontology.RETRIEVAL_PLANS:
        contract = ontology.describe_response_contract(plan_id)
        for key in ("envelope_fields", "control_fields", "field_groups",
                    "assembly_states", "enrichment_outputs"):
            assert key in contract, (
                f"describe_response_contract('{plan_id}') missing '{key}'"
            )


def test_response_contract_assembly_states_match_valid_set():
    """_ASSEMBLY_STATE_DESCRIPTIONS must cover every value in VALID_ASSEMBLY_STATES."""
    contract = ontology.describe_response_contract("customer-billing-context-v4")
    assert set(contract["assembly_states"].keys()) == ontology.VALID_ASSEMBLY_STATES


def test_response_contract_field_groups_match_declared_groups():
    contract = ontology.describe_response_contract("customer-billing-context-v4")
    assert set(contract["field_groups"].keys()) == set(ontology.FIELD_GROUPS.keys())


def test_response_contract_field_group_has_freshness_meta():
    contract = ontology.describe_response_contract("customer-billing-context-v4")
    for fg_name, fg in contract["field_groups"].items():
        meta = fg["meta_freshness"]
        for key in ("field", "confirmed", "stale", "unknown"):
            assert key in meta, f"Field group '{fg_name}' meta_freshness missing '{key}'"


def test_response_contract_field_group_fields_exclude_meta():
    contract = ontology.describe_response_contract("customer-billing-context-v4")
    for fg_name, fg in contract["field_groups"].items():
        assert "_meta" not in fg["fields"], (
            f"Field group '{fg_name}' should not list _meta in fields — "
            "it is described via meta_freshness instead"
        )


def test_context_plan_has_no_enrichment_outputs():
    contract = ontology.describe_response_contract("customer-billing-context-v4")
    assert contract["enrichment_outputs"] == {}, (
        "customer context plan has no enrichment stores"
    )


def test_offers_plan_has_offer_engine_enrichment_output():
    contract = ontology.describe_response_contract("compatible-offers-v1")
    assert "offer_engine" in contract["enrichment_outputs"]
    output_fields = contract["enrichment_outputs"]["offer_engine"]["output_fields"]
    assert "held_skus" in output_fields
    assert "compatible_products" in output_fields


def test_response_contract_is_json_serialisable():
    for plan_id in ontology.RETRIEVAL_PLANS:
        contract = ontology.describe_response_contract(plan_id)
        serialised = json.dumps(contract)
        assert len(serialised) > 0


def test_describe_spec_includes_response_contracts():
    spec = ontology.describe_spec()
    assert "response_contracts" in spec
    for plan_id in ontology.RETRIEVAL_PLANS:
        assert plan_id in spec["response_contracts"]
