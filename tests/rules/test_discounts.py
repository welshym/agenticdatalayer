"""
Discount evaluation tests.

All tests run in-process — no services required.
Covers each policy individually, stacking behaviour, and edge cases
(suspended products, 12-month term, sub-threshold totals).
"""

import pytest

from commercial_rules import evaluate_discounts, reload_policies


@pytest.fixture(autouse=True)
def load_rules():
    """Reload rules from YAML before every test to ensure a clean state."""
    reload_policies()


# ---------------------------------------------------------------------------
# Duration discount (contract_duration_2yr)
# ---------------------------------------------------------------------------

class TestDurationDiscount:

    def test_24_month_product_gets_duration_discount(self, ctx_c001):
        """BB-FIBRE-1G on a 24-month term qualifies for the duration discount."""
        summary = evaluate_discounts(ctx_c001)
        bb = next(p for p in summary.product_discounts if p.sku == "BB-FIBRE-1G")
        assert "contract_duration_2yr" in bb.applied_policies
        assert bb.total_discount_pct == 10.0
        assert bb.discounted_price_gbp == pytest.approx(40.50)

    def test_1_month_product_gets_no_duration_discount(self, ctx_c001):
        """TV-SPORTS-PKG on a 1-month rolling term does not qualify for the duration discount."""
        summary = evaluate_discounts(ctx_c001)
        tv = next(p for p in summary.product_discounts if p.sku == "TV-SPORTS-PKG")
        assert "contract_duration_2yr" not in tv.applied_policies
        assert tv.total_discount_pct == 0.0
        assert tv.discounted_price_gbp == pytest.approx(20.00)

    def test_12_month_product_gets_no_duration_discount(self, ctx_c002):
        """12-month term does not meet the 24-month minimum."""
        summary = evaluate_discounts(ctx_c002)
        mob = next(p for p in summary.product_discounts if p.sku == "MOB-5G-UNLIM")
        assert "contract_duration_2yr" not in mob.applied_policies
        assert mob.total_discount_pct == 0.0

    def test_suspended_products_excluded_from_evaluation(self, ctx_c003):
        """Suspended products are excluded from both the evaluation and the total."""
        summary = evaluate_discounts(ctx_c003)
        evaluated_skus = {p.sku for p in summary.product_discounts}
        assert "MOB-SIM-12GB" not in evaluated_skus

    def test_24_month_active_product_in_mixed_portfolio(self, ctx_c003):
        """Active 24-month BB product qualifies even when the other product is suspended."""
        summary = evaluate_discounts(ctx_c003)
        bb = next(p for p in summary.product_discounts if p.sku == "BB-FTTC-100")
        assert "contract_duration_2yr" in bb.applied_policies
        assert bb.total_discount_pct == 10.0
        assert bb.discounted_price_gbp == pytest.approx(25.20)

    def test_duration_policy_in_applied_list_when_any_product_qualifies(self, ctx_c001):
        """The policy appears in policies_applied when at least one product qualifies."""
        summary = evaluate_discounts(ctx_c001)
        assert "contract_duration_2yr" in summary.policies_applied

    def test_duration_policy_absent_when_no_product_qualifies(self, ctx_c002):
        """The policy is absent from policies_applied when no product meets the term."""
        summary = evaluate_discounts(ctx_c002)
        assert "contract_duration_2yr" not in summary.policies_applied


# ---------------------------------------------------------------------------
# Value discount (high_value_contract)
# ---------------------------------------------------------------------------

class TestValueDiscount:

    def test_total_below_100_no_value_discount(self, ctx_c001):
        """C001 active total £65 — below the £100 threshold."""
        summary = evaluate_discounts(ctx_c001)
        assert "high_value_contract" not in summary.policies_applied

    def test_total_above_100_triggers_value_discount(self, ctx_c004):
        """C004 active total £128 — above the £100 threshold."""
        summary = evaluate_discounts(ctx_c004)
        assert "high_value_contract" in summary.policies_applied

    def test_value_discount_applies_to_every_active_product(self, ctx_c004):
        """When the threshold is met, every active product receives the discount."""
        summary = evaluate_discounts(ctx_c004)
        for pd in summary.product_discounts:
            assert "high_value_contract" in pd.applied_policies

    def test_single_product_below_threshold_no_value_discount(self, ctx_c005):
        """C005 active total £38 — well below the £100 threshold."""
        summary = evaluate_discounts(ctx_c005)
        assert "high_value_contract" not in summary.policies_applied

    def test_suspended_products_excluded_from_threshold_calculation(self, ctx_c003):
        """C003 active total is £28 (suspended mobile excluded) — no value discount."""
        summary = evaluate_discounts(ctx_c003)
        assert "high_value_contract" not in summary.policies_applied


# ---------------------------------------------------------------------------
# Stacking — both policies apply simultaneously
# ---------------------------------------------------------------------------

class TestDiscountStacking:

    def test_tv_24_month_gets_both_discounts(self, ctx_c004):
        """TV-FULL-HSE: 24-month term + total > £100 → 20% combined."""
        summary = evaluate_discounts(ctx_c004)
        tv = next(p for p in summary.product_discounts if p.sku == "TV-FULL-HSE")
        assert "contract_duration_2yr" in tv.applied_policies
        assert "high_value_contract" in tv.applied_policies
        assert tv.total_discount_pct == 20.0
        assert tv.discounted_price_gbp == pytest.approx(44.00)

    def test_bb_24_month_gets_both_discounts(self, ctx_c004):
        """BB-FIBRE-500: 24-month term + total > £100 → 20% combined."""
        summary = evaluate_discounts(ctx_c004)
        bb = next(p for p in summary.product_discounts if p.sku == "BB-FIBRE-500")
        assert "contract_duration_2yr" in bb.applied_policies
        assert "high_value_contract" in bb.applied_policies
        assert bb.total_discount_pct == 20.0
        assert bb.discounted_price_gbp == pytest.approx(30.40)

    def test_12_month_product_gets_only_value_discount(self, ctx_c004):
        """MOB-5G-UNLIM: 12-month mobile — only the high-value discount applies."""
        summary = evaluate_discounts(ctx_c004)
        mob = next(p for p in summary.product_discounts if p.sku == "MOB-5G-UNLIM")
        assert "contract_duration_2yr" not in mob.applied_policies
        assert "high_value_contract" in mob.applied_policies
        assert mob.total_discount_pct == 10.0
        assert mob.discounted_price_gbp == pytest.approx(31.50)

    def test_c004_total_discounted_monthly(self, ctx_c004):
        """C004 discounted total: TV £44.00 + BB £30.40 + MOB £31.50 = £105.90."""
        summary = evaluate_discounts(ctx_c004)
        assert summary.total_list_monthly_gbp == pytest.approx(128.00)
        assert summary.total_discounted_monthly_gbp == pytest.approx(105.90)
        assert summary.total_saving_gbp == pytest.approx(22.10)


# ---------------------------------------------------------------------------
# No discount scenarios
# ---------------------------------------------------------------------------

class TestNoDiscount:

    def test_c002_no_discounts(self, ctx_c002):
        """C002: 12-month term, £35 total — neither policy applies."""
        summary = evaluate_discounts(ctx_c002)
        assert summary.policies_applied == []
        assert summary.total_saving_gbp == pytest.approx(0.0)
        assert summary.total_discounted_monthly_gbp == pytest.approx(35.00)

    def test_c005_no_discounts(self, ctx_c005):
        """C005: 1-month term, £38 total — neither policy applies."""
        summary = evaluate_discounts(ctx_c005)
        assert summary.policies_applied == []
        assert summary.total_saving_gbp == pytest.approx(0.0)
        assert summary.total_discounted_monthly_gbp == pytest.approx(38.00)

    def test_empty_portfolio_no_error(self):
        """An empty product list produces a zero-value summary without error."""
        summary = evaluate_discounts({"commercial_state": {"products": []}})
        assert summary.policies_applied == []
        assert summary.total_list_monthly_gbp == 0.0
        assert summary.total_saving_gbp == 0.0
