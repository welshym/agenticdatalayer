"""
Customer context fixtures for discount rule tests.
Each fixture mirrors a seeded customer from seed/billing.yaml in the canonical
post-assembly format produced by CDC (list_price_gbp = catalogue price used for
rule evaluation; monthly_charge_gbp = contracted price the customer pays).
contract_term_months is the canonical field name after CDC assembly.
"""

import pytest


@pytest.fixture
def ctx_c001():
    """C001: BB-FIBRE-1G (24mo, list £45, contracted £40.50) + TV-SPORTS-PKG (1mo, list £20). Active list total £65."""
    return {
        "customer_id": "C001",
        "commercial_state": {
            "subscriptions": [
                {
                    "product_id": "BB-FIBRE-1G",
                    "product_type": "broadband",
                    "status": "active",
                    "list_price_gbp": 45.00,
                    "monthly_charge_gbp": 40.50,
                    "contract_term_months": 24,
                    "effective_date": "2024-01-15",
                },
                {
                    "product_id": "TV-SPORTS-PKG",
                    "product_type": "tv",
                    "status": "active",
                    "list_price_gbp": 20.00,
                    "monthly_charge_gbp": 20.00,
                    "contract_term_months": 1,
                    "effective_date": "2024-03-01",
                },
            ]
        },
    }


@pytest.fixture
def ctx_c002():
    """C002: MOB-5G-UNLIM (12mo, list £35). Active list total £35."""
    return {
        "customer_id": "C002",
        "commercial_state": {
            "subscriptions": [
                {
                    "product_id": "MOB-5G-UNLIM",
                    "product_type": "mobile",
                    "status": "active",
                    "list_price_gbp": 35.00,
                    "monthly_charge_gbp": 35.00,
                    "contract_term_months": 12,
                    "effective_date": "2023-11-01",
                },
            ]
        },
    }


@pytest.fixture
def ctx_c003():
    """C003: BB-FTTC-100 (24mo, list £28, contracted £25.20, active) + MOB-SIM-12GB (1mo, list £12, suspended)."""
    return {
        "customer_id": "C003",
        "commercial_state": {
            "subscriptions": [
                {
                    "product_id": "BB-FTTC-100",
                    "product_type": "broadband",
                    "status": "active",
                    "list_price_gbp": 28.00,
                    "monthly_charge_gbp": 25.20,
                    "contract_term_months": 24,
                    "effective_date": "2023-06-10",
                },
                {
                    "product_id": "MOB-SIM-12GB",
                    "product_type": "mobile",
                    "status": "suspended",
                    "list_price_gbp": 12.00,
                    "monthly_charge_gbp": 12.00,
                    "contract_term_months": 1,
                    "effective_date": "2024-02-01",
                },
            ]
        },
    }


@pytest.fixture
def ctx_c004():
    """C004: TV-FULL-HSE (24mo, list £55) + BB-FIBRE-500 (24mo, list £38) + MOB-5G-UNLIM (12mo, list £35). Active list total £128."""
    return {
        "customer_id": "C004",
        "commercial_state": {
            "subscriptions": [
                {
                    "product_id": "TV-FULL-HSE",
                    "product_type": "tv",
                    "status": "active",
                    "list_price_gbp": 55.00,
                    "monthly_charge_gbp": 44.00,
                    "contract_term_months": 24,
                    "effective_date": "2022-09-15",
                },
                {
                    "product_id": "BB-FIBRE-500",
                    "product_type": "broadband",
                    "status": "active",
                    "list_price_gbp": 38.00,
                    "monthly_charge_gbp": 30.40,
                    "contract_term_months": 24,
                    "effective_date": "2022-09-15",
                },
                {
                    "product_id": "MOB-5G-UNLIM",
                    "product_type": "mobile",
                    "status": "active",
                    "list_price_gbp": 35.00,
                    "monthly_charge_gbp": 31.50,
                    "contract_term_months": 12,
                    "effective_date": "2023-01-20",
                },
            ]
        },
    }


@pytest.fixture
def ctx_c005():
    """C005: BB-FIBRE-500 (1mo, list £38). Active list total £38."""
    return {
        "customer_id": "C005",
        "commercial_state": {
            "subscriptions": [
                {
                    "product_id": "BB-FIBRE-500",
                    "product_type": "broadband",
                    "status": "active",
                    "list_price_gbp": 38.00,
                    "monthly_charge_gbp": 38.00,
                    "contract_term_months": 1,
                    "effective_date": "2024-05-01",
                },
            ]
        },
    }
