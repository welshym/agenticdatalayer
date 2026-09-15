"""
Catalogue combination-validation unit tests.

All tests run in-process — no services required.
Covers the REQUIRES relationship type: prerequisite enforcement, OR semantics
(any one target satisfies the requirement), and interaction with UPGRADES_TO.
"""

import pytest
import catalogue_app


@pytest.fixture(autouse=True)
def load_catalogue():
    """Reload the catalogue from YAML before every test for a clean state."""
    catalogue_app._load_catalogue()


# ---------------------------------------------------------------------------
# REQUIRES — basic enforcement
# ---------------------------------------------------------------------------

class TestRequiresEnforcement:

    def test_hardware_without_broadband_is_invalid(self):
        """HW-WIFI-BOOSTER without any broadband product should fail validation."""
        valid, violations = catalogue_app._is_combination_valid(["HW-WIFI-BOOSTER"])
        assert not valid
        assert any("HW-WIFI-BOOSTER" in v for v in violations)

    def test_hardware_with_broadband_is_valid(self):
        """HW-WIFI-BOOSTER alongside any broadband product should pass."""
        valid, violations = catalogue_app._is_combination_valid(
            ["HW-WIFI-BOOSTER", "BB-FIBRE-500"]
        )
        assert valid, violations

    def test_streaming_without_tv_is_invalid(self):
        """STRM-PLAY-STD without a TV package should fail validation."""
        valid, violations = catalogue_app._is_combination_valid(["STRM-PLAY-STD"])
        assert not valid
        assert any("STRM-PLAY-STD" in v for v in violations)

    def test_streaming_with_tv_package_is_valid(self):
        """STRM-PLAY-STD alongside a TV package should pass."""
        valid, violations = catalogue_app._is_combination_valid(
            ["STRM-PLAY-STD", "TV-SPORTS-PKG"]
        )
        assert valid, violations

    def test_streaming_with_full_house_tv_is_valid(self):
        """STRM-PLAY-STD accepts TV-FULL-HSE as the prerequisite."""
        valid, violations = catalogue_app._is_combination_valid(
            ["STRM-PLAY-STD", "TV-FULL-HSE"]
        )
        assert valid, violations


# ---------------------------------------------------------------------------
# REQUIRES — OR semantics (any one prerequisite satisfies)
# ---------------------------------------------------------------------------

class TestRequiresOrSemantics:

    def test_any_broadband_tier_satisfies_booster(self):
        """All three broadband tiers independently satisfy HW-WIFI-BOOSTER's requirement."""
        for bb_sku in ("BB-FTTC-100", "BB-FIBRE-500", "BB-FIBRE-1G"):
            valid, violations = catalogue_app._is_combination_valid(
                ["HW-WIFI-BOOSTER", bb_sku]
            )
            assert valid, f"{bb_sku} should satisfy HW-WIFI-BOOSTER requires: {violations}"

    def test_any_tv_package_satisfies_streaming_addons(self):
        """Both TV packages independently satisfy the streaming add-on requirement."""
        for tv_sku in ("TV-SPORTS-PKG", "TV-FULL-HSE"):
            for strm_sku in ("STRM-PLAY-STD", "STRM-FAMILY", "STRM-MOVIES"):
                valid, violations = catalogue_app._is_combination_valid([strm_sku, tv_sku])
                assert valid, f"{tv_sku} should satisfy {strm_sku}: {violations}"

    def test_multiple_streaming_addons_with_one_tv_package(self):
        """Multiple streaming add-ons with one TV package should all pass together."""
        valid, violations = catalogue_app._is_combination_valid([
            "TV-FULL-HSE",
            "STRM-PLAY-STD",
            "STRM-FAMILY",
            "STRM-MOVIES",
        ])
        assert valid, violations


# ---------------------------------------------------------------------------
# REQUIRES — violation message content
# ---------------------------------------------------------------------------

class TestRequiresViolationMessage:

    def test_violation_names_the_product(self):
        """The violation message identifies the product missing its prerequisite."""
        _, violations = catalogue_app._is_combination_valid(["STRM-FAMILY"])
        assert any("STRM-FAMILY" in v for v in violations)

    def test_violation_lists_options(self):
        """The violation message lists the products that would satisfy the requirement."""
        _, violations = catalogue_app._is_combination_valid(["STRM-FAMILY"])
        assert any("TV-FULL-HSE" in v or "TV-SPORTS-PKG" in v for v in violations)


# ---------------------------------------------------------------------------
# UPGRADES_TO mutual exclusivity — Stream and Glass
# ---------------------------------------------------------------------------

class TestTvHardwareMutualExclusivity:

    def test_tv_pod_and_smart_tv_cannot_be_held_together(self):
        """HW-TV-POD and HW-SMART-TV are on the same upgrade path and cannot coexist."""
        valid, violations = catalogue_app._is_combination_valid([
            "BB-FIBRE-500", "HW-TV-POD", "HW-SMART-TV"
        ])
        assert not valid
        assert any("HW-SMART-TV" in v and "HW-TV-POD" in v for v in violations)

    def test_smart_tv_alone_with_broadband_is_valid(self):
        """HW-SMART-TV with broadband but without TV Pod should pass."""
        valid, violations = catalogue_app._is_combination_valid(
            ["BB-FIBRE-1G", "HW-SMART-TV"]
        )
        assert valid, violations

    def test_tv_pod_alone_with_broadband_is_valid(self):
        """HW-TV-POD with broadband but without Smart TV should pass."""
        valid, violations = catalogue_app._is_combination_valid(
            ["BB-FTTC-100", "HW-TV-POD"]
        )
        assert valid, violations


# ---------------------------------------------------------------------------
# Full realistic portfolio
# ---------------------------------------------------------------------------

class TestRealisticPortfolio:

    def test_broadband_tv_streaming_hardware_is_valid(self):
        """A realistic quad-play-style portfolio with hardware and streaming should be valid."""
        valid, violations = catalogue_app._is_combination_valid([
            "BB-FIBRE-500",
            "TV-FULL-HSE",
            "MOB-5G-UNLIM",
            "HW-WIFI-BOOSTER",
            "HW-TV-POD",
            "STRM-PLAY-STD",
            "STRM-FAMILY",
        ])
        assert valid, violations

    def test_empty_combination_is_valid(self):
        """An empty SKU list has no violations."""
        valid, violations = catalogue_app._is_combination_valid([])
        assert valid
        assert violations == []
