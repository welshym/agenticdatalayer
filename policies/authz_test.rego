package authz

import rego.v1

# ---------------------------------------------------------------------------
# allow — permitted callers and intents
# ---------------------------------------------------------------------------

test_purchase_agent_add_subscription_allowed if {
    allow with input as {"caller_id": "purchase-agent", "intent": "add_subscription"}
        with data.callers as mock_callers
}

test_purchase_agent_cancel_subscription_allowed if {
    allow with input as {"caller_id": "purchase-agent", "intent": "cancel_subscription"}
        with data.callers as mock_callers
}

test_catalogue_admin_update_price_allowed if {
    allow with input as {"caller_id": "catalogue-admin", "intent": "update_product_price"}
        with data.callers as mock_callers
}

test_catalogue_admin_recalculate_billing_allowed if {
    allow with input as {"caller_id": "catalogue-admin", "intent": "recalculate_billing"}
        with data.callers as mock_callers
}

test_system_admin_all_intents_allowed if {
    allow with input as {"caller_id": "system-admin", "intent": "add_subscription"}
        with data.callers as mock_callers
    allow with input as {"caller_id": "system-admin", "intent": "cancel_subscription"}
        with data.callers as mock_callers
    allow with input as {"caller_id": "system-admin", "intent": "update_product_price"}
        with data.callers as mock_callers
    allow with input as {"caller_id": "system-admin", "intent": "recalculate_billing"}
        with data.callers as mock_callers
}

# ---------------------------------------------------------------------------
# allow — denied callers and intents
# ---------------------------------------------------------------------------

test_purchase_agent_update_price_denied if {
    not allow with input as {"caller_id": "purchase-agent", "intent": "update_product_price"}
        with data.callers as mock_callers
}

test_purchase_agent_recalculate_denied if {
    not allow with input as {"caller_id": "purchase-agent", "intent": "recalculate_billing"}
        with data.callers as mock_callers
}

test_catalogue_admin_add_subscription_denied if {
    not allow with input as {"caller_id": "catalogue-admin", "intent": "add_subscription"}
        with data.callers as mock_callers
}

test_catalogue_admin_cancel_subscription_denied if {
    not allow with input as {"caller_id": "catalogue-admin", "intent": "cancel_subscription"}
        with data.callers as mock_callers
}

test_unknown_caller_denied if {
    not allow with input as {"caller_id": "unknown-agent", "intent": "add_subscription"}
        with data.callers as mock_callers
}

test_empty_caller_denied if {
    not allow with input as {"caller_id": "", "intent": "add_subscription"}
        with data.callers as mock_callers
}

# ---------------------------------------------------------------------------
# allowed_intents — returns the correct list per caller
# ---------------------------------------------------------------------------

test_purchase_agent_allowed_intents if {
    intents := allowed_intents with input as {"caller_id": "purchase-agent"}
        with data.callers as mock_callers
    intents == ["add_subscription", "cancel_subscription"]
}

test_catalogue_admin_allowed_intents if {
    intents := allowed_intents with input as {"caller_id": "catalogue-admin"}
        with data.callers as mock_callers
    intents == ["update_product_price", "recalculate_billing"]
}

test_unknown_caller_allowed_intents_empty if {
    intents := allowed_intents with input as {"caller_id": "ghost-agent"}
        with data.callers as mock_callers
    intents == []
}

# ---------------------------------------------------------------------------
# Shared mock data (mirrors policies/data.json)
# ---------------------------------------------------------------------------

mock_callers := {
    "purchase-agent": {
        "description": "Customer-facing purchase agent.",
        "allowed_intents": ["add_subscription", "cancel_subscription"]
    },
    "catalogue-admin": {
        "description": "Product catalogue administrator.",
        "allowed_intents": ["update_product_price", "recalculate_billing"]
    },
    "system-admin": {
        "description": "System administrator.",
        "allowed_intents": [
            "add_subscription",
            "cancel_subscription",
            "update_product_price",
            "recalculate_billing"
        ]
    }
}
