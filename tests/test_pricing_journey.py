"""
test_pricing_journey.py — Action Broker pricing journey integration tests
=========================================================================

Covers the pricing scenario extracted from the old demo_journey.py:
  1. Permission denial — purchase-agent cannot update product price
  2. Permission denial — purchase-agent cannot trigger billing recalculation
  3. A valid JWT with no write permissions (ui-client) is rejected with 403
  4. Denied intents appear in the audit log
  5. Catalogue price update via catalogue-admin (Action Broker)
  6. Billing charge unchanged after catalogue update (before recalculation)
  7. Billing charge updated after recalculate_billing intent

Requires all services to be running:
  ./start.sh

Tests are automatically skipped if the Action Broker (8018) is not reachable.
All state changes are fully restored in teardown so tests are idempotent.
"""
from __future__ import annotations

import sys
import os
import time

import httpx
import pytest

# Make the demo package importable when running pytest from the tests/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import auth as _auth  # noqa: E402

ACTION_BROKER_URL = "http://localhost:8018"
ACG_URL           = "http://localhost:8013"
BILLING_URL       = "http://localhost:8017"
CATALOGUE_URL     = "http://localhost:8014"

# C001 holds BB-FIBRE-1G in the seed data.
CUSTOMER_ID = "C001"
PRICE_SKU   = "BB-FIBRE-1G"
NEW_PRICE   = 52.00


# ---------------------------------------------------------------------------
# Pre-minted JWT headers — minted directly using the auth module so tests
# don't depend on the Action Broker /token endpoint for token acquisition.
# In production, tokens would be acquired via the /token endpoint.
# ---------------------------------------------------------------------------

def _bearer(client_id: str) -> dict:
    """Return an Authorization header dict for a registered demo client."""
    return {"Authorization": f"Bearer {_auth.mint_token(client_id)}"}


_PURCHASE_HEADERS  = _bearer("purchase-agent")
_ADMIN_HEADERS     = _bearer("catalogue-admin")
_SYSADMIN_HEADERS  = _bearer("system-admin")
_UI_HEADERS        = _bearer("ui-client")


# ---------------------------------------------------------------------------
# Connectivity guard — skip entire module if Action Broker is down
# ---------------------------------------------------------------------------

def _services_up() -> bool:
    try:
        r = httpx.get(f"{ACTION_BROKER_URL}/health", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _services_up(),
    reason="Action Broker not reachable — run ./start.sh before running integration tests",
)


# ---------------------------------------------------------------------------
# Module-scoped fixtures — snapshot state once, restore after all tests run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def original_catalogue_price() -> float:
    """Capture the catalogue list price for PRICE_SKU before any test mutates it."""
    resp = httpx.get(f"{CATALOGUE_URL}/products/{PRICE_SKU}", timeout=5.0)
    resp.raise_for_status()
    return resp.json()["list_price_gbp"]


@pytest.fixture(scope="module")
def original_billing_charges() -> dict[str, float]:
    """Capture per-customer billed monthly charges for PRICE_SKU across all customers."""
    resp = httpx.get(f"{BILLING_URL}/customers", timeout=5.0)
    resp.raise_for_status()
    charges: dict[str, float] = {}
    for cid in [c["customer_id"] for c in resp.json()]:
        r = httpx.get(f"{BILLING_URL}/customers/{cid}", timeout=5.0)
        if r.status_code != 200:
            continue
        for sub in r.json().get("sub_lines", []):
            if sub.get("sku") == PRICE_SKU and sub.get("stat") != "C":
                charges[cid] = sub["monthly_charge"]
    return charges


@pytest.fixture(scope="module", autouse=True)
def restore_state(original_catalogue_price: float, original_billing_charges: dict[str, float]):
    """Restore catalogue price and billing charges after all tests in this module run."""
    yield
    # Restore catalogue price via system-admin through the Action Broker (JWT required)
    httpx.post(
        f"{ACTION_BROKER_URL}/submit-intent",
        json={
            "intent":  "update_product_price",
            "payload": {"sku": PRICE_SKU, "new_list_price_gbp": original_catalogue_price},
        },
        headers=_SYSADMIN_HEADERS,
        timeout=10.0,
    )
    # Restore each customer's billed charge directly on the Billing SoR (internal — no JWT)
    for cid, charge in original_billing_charges.items():
        httpx.patch(
            f"{BILLING_URL}/customers/{cid}",
            json={"update": {"product_sku": PRICE_SKU, "new_monthly_charge": charge}},
            timeout=10.0,
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_billed_charge(customer_id: str, sku: str, headers: dict) -> float | None:
    """Read the current contracted monthly charge for a SKU from the ACG context."""
    resp = httpx.get(
        f"{ACG_URL}/context/{customer_id}", headers=headers, timeout=10.0
    )
    resp.raise_for_status()
    subs = (resp.json().get("commercial_state") or {}).get("subscriptions", [])
    sub = next((s for s in subs if s.get("product_id") == sku), None)
    return sub.get("monthly_charge_gbp") if sub else None


# ---------------------------------------------------------------------------
# Permission denial tests
# ---------------------------------------------------------------------------

class TestPermissionDenials:
    """purchase-agent must be denied intents outside its allowed list."""

    def test_purchase_agent_cannot_update_product_price(self):
        resp = httpx.post(
            f"{ACTION_BROKER_URL}/submit-intent",
            json={
                "intent":  "update_product_price",
                "payload": {"sku": PRICE_SKU, "new_list_price_gbp": 99.00},
            },
            headers=_PURCHASE_HEADERS,
            timeout=10.0,
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        detail = resp.json()["detail"]
        assert detail["error"] == "permission_denied"
        assert "update_product_price" in detail["message"]
        assert "audit_id" in detail

    def test_purchase_agent_cannot_recalculate_billing(self):
        resp = httpx.post(
            f"{ACTION_BROKER_URL}/submit-intent",
            json={
                "intent":  "recalculate_billing",
                "payload": {"product_sku": PRICE_SKU},
            },
            headers=_PURCHASE_HEADERS,
            timeout=10.0,
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["error"] == "permission_denied"
        assert "recalculate_billing" in detail["message"]

    def test_ui_client_cannot_add_subscription(self):
        """ui-client holds a valid JWT but has no write permissions — must be denied."""
        resp = httpx.post(
            f"{ACTION_BROKER_URL}/submit-intent",
            json={
                "intent":      "add_subscription",
                "customer_id": CUSTOMER_ID,
                "payload":     {"sku": PRICE_SKU},
            },
            headers=_UI_HEADERS,
            timeout=10.0,
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        detail = resp.json()["detail"]
        assert detail["error"] == "permission_denied"
        assert "audit_id" in detail

    def test_denied_intents_appear_in_audit_log(self):
        """Every denial must be recorded — tests above must have populated the audit log."""
        audit_resp = httpx.get(
            f"{ACTION_BROKER_URL}/audit?limit=50",
            headers=_SYSADMIN_HEADERS,
            timeout=5.0,
        )
        audit_resp.raise_for_status()
        denied = [e for e in audit_resp.json() if e["outcome"] == "denied"]
        assert len(denied) > 0, "Expected at least one denied audit entry from preceding tests"


# ---------------------------------------------------------------------------
# Pricing journey tests — must run in order: 01 → 02 → 03
# ---------------------------------------------------------------------------

class TestPricingJourney:
    """
    Three-phase pricing scenario.
    Each test depends on state established by the previous one.
    Named test_01/02/03 so pytest collects them in declaration order.
    """

    def test_01_catalogue_price_update_via_broker(self, original_catalogue_price: float):
        """catalogue-admin updates PRICE_SKU to NEW_PRICE via the Action Broker."""
        resp = httpx.post(
            f"{ACTION_BROKER_URL}/submit-intent",
            json={
                "intent":  "update_product_price",
                "payload": {"sku": PRICE_SKU, "new_list_price_gbp": NEW_PRICE},
            },
            headers=_ADMIN_HEADERS,
            timeout=10.0,
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        result = resp.json()
        assert result["outcome"] == "permitted"
        assert result["target_sor"] == "catalogue-sor"
        assert "audit_id" in result

        # Confirm the catalogue SoR reflects the new price immediately
        cat_resp = httpx.get(f"{CATALOGUE_URL}/products/{PRICE_SKU}", timeout=5.0)
        cat_resp.raise_for_status()
        assert cat_resp.json()["list_price_gbp"] == NEW_PRICE, (
            f"Catalogue did not update to {NEW_PRICE}"
        )

    def test_02_billing_charge_unchanged_before_recalculate(
        self,
        original_billing_charges: dict[str, float],
    ):
        """
        After a catalogue price update only, the customer's contracted monthly
        charge must be unchanged. Catalogue metadata updates do not reprice
        billing records — that requires a separate recalculate_billing intent.
        """
        if CUSTOMER_ID not in original_billing_charges:
            pytest.skip(f"{CUSTOMER_ID} does not hold {PRICE_SKU} — skipping billing isolation check")

        # Allow CDC fan-out from the catalogue update to settle
        time.sleep(1.5)

        charge = _get_billed_charge(CUSTOMER_ID, PRICE_SKU, _ADMIN_HEADERS)
        original_charge = original_billing_charges[CUSTOMER_ID]

        assert charge is not None, (
            f"{PRICE_SKU} not found in {CUSTOMER_ID} context after catalogue update"
        )
        assert abs(charge - original_charge) < 0.01, (
            f"Billed charge changed from £{original_charge:.2f} to £{charge:.2f} after "
            f"catalogue update alone — billing records must not change until "
            f"recalculate_billing is submitted"
        )

    def test_03_billing_charge_updated_after_recalculate(
        self,
        original_billing_charges: dict[str, float],
    ):
        """
        After catalogue-admin submits recalculate_billing, the customer's contracted
        charge must reflect the new catalogue list price (or lower if discounts fire).
        """
        if CUSTOMER_ID not in original_billing_charges:
            pytest.skip(f"{CUSTOMER_ID} does not hold {PRICE_SKU}")

        resp = httpx.post(
            f"{ACTION_BROKER_URL}/submit-intent",
            json={
                "intent":  "recalculate_billing",
                "payload": {"product_sku": PRICE_SKU},
            },
            headers=_ADMIN_HEADERS,
            timeout=15.0,
        )
        assert resp.status_code == 200, f"Recalculate failed: {resp.text}"
        result = resp.json()
        assert result["outcome"] == "permitted"
        assert result.get("customers_updated", 0) > 0, (
            "Expected at least one customer to be updated by recalculate_billing"
        )

        # Allow CDC events from the recalculation to propagate into the cache
        time.sleep(1.5)

        charge_after = _get_billed_charge(CUSTOMER_ID, PRICE_SKU, _ADMIN_HEADERS)
        assert charge_after is not None, (
            f"{PRICE_SKU} not found in {CUSTOMER_ID} context after recalculation"
        )

        # Charge must not exceed the new list price (discounts may reduce it further)
        assert charge_after <= NEW_PRICE, (
            f"Recalculated charge £{charge_after:.2f} exceeds new list price £{NEW_PRICE:.2f}"
        )

        # Charge must differ from the original (proving recalculation took effect)
        original_charge = original_billing_charges[CUSTOMER_ID]
        assert abs(charge_after - original_charge) > 0.01, (
            f"Charge unchanged at £{charge_after:.2f} after recalculation from "
            f"original £{original_charge:.2f} to new list price £{NEW_PRICE:.2f} — "
            f"CDC may still be processing, or the new price equals the original"
        )
