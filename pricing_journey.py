"""
pricing_journey.py — Catalogue price update and billing recalculation journey
==============================================================================

Demonstrates the catalogue-admin write path through the Action Broker:

  1. Read current state — catalogue list price and customer's billed charge (ACG)
  2. Update catalogue price via Action Broker (update_product_price intent)
  3. Re-query ACG — confirm billed charge is UNCHANGED (catalogue metadata update
     only; billing contracts are not repriced until recalculation is requested)
  4. Trigger billing recalculation via Action Broker (recalculate_billing intent)
  5. Re-query ACG — confirm billed charge HAS updated to reflect the new price

Teardown restores the original catalogue price and billing charges so the script
is safe to run repeatedly without restarting services.

Use --client-id to test permission enforcement:
  catalogue-admin  (default) — permitted: update_product_price, recalculate_billing
  purchase-agent             — denied at step 2 (not in allowed intents)
  system-admin               — permitted: full write access
  ui-client                  — denied at step 2 (read-only; no write permissions)

Usage:
  python pricing_journey.py                                         # C001, BB-FIBRE-1G
  python pricing_journey.py C002 --sku BB-FIBRE-250M --new-price 38
  python pricing_journey.py --client-id purchase-agent              # permission denied

Requires all services to be running:
  ./start.sh
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

import httpx

# ── Service base URLs ───────────────────────────────────────────────────────
ACG_URL           = "http://localhost:8013"
ACTION_BROKER_URL = "http://localhost:8018"
CATALOGUE_URL     = "http://localhost:8014"
BILLING_URL       = "http://localhost:8017"   # teardown only (direct SoR restore)

PROPAGATION_WAIT_S = 1.5


# ===========================================================================
# Formatting helpers
# ===========================================================================

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
GREY   = "\033[90m"
RED    = "\033[31m"


def _hdr(n: int, title: str) -> None:
    bar = "─" * 62
    print(f"\n{BOLD}{CYAN}{bar}{RESET}")
    print(f"{BOLD}{CYAN}  Step {n}: {title}{RESET}")
    print(f"{BOLD}{CYAN}{bar}{RESET}")


def _ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def _info(msg: str) -> None:
    print(f"  {GREY}→{RESET} {msg}")


def _warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}")


def _err(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}", file=sys.stderr)


def _denied(detail: dict) -> None:
    print(f"\n  {RED}{'─' * 58}{RESET}")
    print(f"  {BOLD}{RED}  Action Broker: Intent Denied{RESET}")
    print(f"  {RED}{'─' * 58}{RESET}")
    print(f"  {RED}intent    :{RESET}  {detail.get('intent', '?')}")
    print(f"  {RED}reason    :{RESET}  {detail.get('message', detail.get('error', '?'))}")
    allowed = detail.get("allowed_intents")
    if allowed is not None:
        print(f"  {GREY}allowed   :{RESET}  {', '.join(allowed) if allowed else '(none — read-only)'}")
    print(f"  {GREY}audit_id  :{RESET}  {detail.get('audit_id', 'n/a')}")
    print(f"  {RED}{'─' * 58}{RESET}")


def _print_subscription(sub: dict | None, sku: str, label: str = "") -> None:
    prefix = f"  {label}: " if label else "  "
    if sub is None:
        print(f"{prefix}{GREY}{sku} not found in holdings{RESET}")
        return
    charge = sub.get("monthly_charge_gbp", "?")
    charge_str = f"£{charge:.2f}/mo" if isinstance(charge, (int, float)) else str(charge)
    discount_pct = sub.get("total_discount_pct", 0.0)
    discount_str = (
        f"  {GREY}({discount_pct:.1f}% discount applied){RESET}"
        if discount_pct and discount_pct > 0.01 else ""
    )
    status_colour = GREEN if sub.get("status") == "active" else YELLOW
    print(
        f"    {BOLD}{sku}{RESET}  "
        f"{status_colour}{sub.get('status', '?')}{RESET}  "
        f"{BOLD}{charge_str}{RESET}"
        f"{discount_str}"
    )


def _find_subscription(context: dict, sku: str) -> dict | None:
    subs = (context.get("commercial_state") or {}).get("subscriptions", [])
    return next((s for s in subs if s.get("product_id") == sku), None)


# ===========================================================================
# Setup / teardown
# ===========================================================================

@dataclass
class _Snapshot:
    original_list_price: float
    # Per-customer original billed monthly charges for the SKU.
    # POST /recalculate affects all holders, so we capture and restore all of them.
    original_billing_charges: dict[str, float] = field(default_factory=dict)


def _teardown_hdr(title: str) -> None:
    bar = "─" * 62
    print(f"\n{BOLD}{GREY}{bar}{RESET}")
    print(f"{BOLD}{GREY}  Teardown: {title}{RESET}")
    print(f"{BOLD}{GREY}{bar}{RESET}")


async def _snapshot(client: httpx.AsyncClient, sku: str) -> _Snapshot:
    """Capture catalogue list price and per-customer billing charges before the demo."""
    _teardown_hdr("snapshotting state before run")

    cat_resp = await client.get(f"{CATALOGUE_URL}/products/{sku}")
    cat_resp.raise_for_status()
    original_price: float = cat_resp.json()["list_price_gbp"]
    _ok(f"Catalogue list price for {sku}: £{original_price:.2f}/mo")

    # Capture billing charges across all customers holding this SKU.
    # recalculate_billing affects every holder, so we must restore all of them.
    cust_resp = await client.get(f"{BILLING_URL}/customers")
    cust_resp.raise_for_status()
    all_ids = [c["customer_id"] for c in cust_resp.json()]

    charges: dict[str, float] = {}
    for cid in all_ids:
        r = await client.get(f"{BILLING_URL}/customers/{cid}")
        if r.status_code != 200:
            continue
        for sub in r.json().get("sub_lines", []):
            if sub.get("sku") == sku and sub.get("stat") != "C":
                charges[cid] = sub["monthly_charge"]

    if charges:
        holders = ", ".join(f"{cid}=£{c:.2f}" for cid, c in charges.items())
        _ok(f"Active holders of {sku}: {holders}")
    else:
        _info(f"No active holders of {sku} found in Billing SoR")

    return _Snapshot(original_list_price=original_price, original_billing_charges=charges)


async def _teardown(
    client: httpx.AsyncClient,
    sku: str,
    snapshot: _Snapshot,
    auth_headers: dict,
) -> None:
    """
    Restore catalogue price via Action Broker (system-admin) and billing charges
    directly via the Billing SoR (SoR does not require JWT).
    """
    _teardown_hdr("restoring state")

    # Restore catalogue price — always use system-admin regardless of journey client
    try:
        tok = await client.post(
            f"{ACTION_BROKER_URL}/token",
            json={"client_id": "system-admin"},
        )
        admin_headers = {"Authorization": f"Bearer {tok.json()['access_token']}"}
    except Exception:
        admin_headers = auth_headers  # fallback if token endpoint unreachable

    try:
        resp = await client.post(
            f"{ACTION_BROKER_URL}/submit-intent",
            json={
                "intent":  "update_product_price",
                "payload": {"sku": sku, "new_list_price_gbp": snapshot.original_list_price},
            },
            headers=admin_headers,
        )
        resp.raise_for_status()
        _ok(f"Restored {sku} catalogue list price to £{snapshot.original_list_price:.2f}/mo")
    except Exception as exc:
        _warn(f"Could not restore catalogue price: {exc}")

    # Restore each customer's billed charge directly via Billing SoR (no JWT needed)
    for cid, original_charge in snapshot.original_billing_charges.items():
        try:
            resp = await client.patch(
                f"{BILLING_URL}/customers/{cid}",
                json={"update": {"product_sku": sku, "new_monthly_charge": original_charge}},
            )
            resp.raise_for_status()
            _ok(f"Restored {cid} billing for {sku} to £{original_charge:.2f}/mo")
        except Exception as exc:
            _warn(f"Could not restore billing for {cid}/{sku}: {exc}")

    _info(f"Waiting {PROPAGATION_WAIT_S}s for teardown CDC events to propagate…")
    await asyncio.sleep(PROPAGATION_WAIT_S)
    _ok("State restored — safe to run again")


# ===========================================================================
# Journey steps
# ===========================================================================

async def step1_read_current_state(
    client: httpx.AsyncClient,
    customer_id: str,
    sku: str,
    auth_headers: dict,
) -> tuple[float, float | None]:
    """
    Read the current catalogue list price and the customer's billed charge for the SKU.
    Returns (list_price, billed_charge_or_None).
    """
    _hdr(1, f"Read current state — {sku}")

    cat_resp = await client.get(f"{CATALOGUE_URL}/products/{sku}")
    cat_resp.raise_for_status()
    product = cat_resp.json()
    list_price: float = product["list_price_gbp"]
    _ok(f"Catalogue list price:  £{list_price:.2f}/mo")

    _info(f"GET {ACG_URL}/context/{customer_id}")
    ctx_resp = await client.get(f"{ACG_URL}/context/{customer_id}", headers=auth_headers)
    ctx_resp.raise_for_status()
    ctx = ctx_resp.json()

    sub = _find_subscription(ctx, sku)
    if sub:
        billed = sub.get("monthly_charge_gbp")
        _ok(f"Customer {customer_id} billed charge: £{billed:.2f}/mo")
        _print_subscription(sub, sku)
    else:
        billed = None
        _warn(f"{sku} not found in {customer_id} holdings — billing isolation step will be skipped")

    return list_price, (sub.get("monthly_charge_gbp") if sub else None)


async def step2_update_catalogue_price(
    client: httpx.AsyncClient,
    sku: str,
    new_price: float,
    auth_headers: dict,
) -> bool:
    """
    Submit update_product_price to the Action Broker.
    Returns True on success, False if denied (prints denial and returns).
    """
    _hdr(2, f"Update catalogue price — {sku} → £{new_price:.2f}/mo")
    _info(
        f"POST {ACTION_BROKER_URL}/submit-intent  "
        f"intent=update_product_price  sku={sku}  new_list_price_gbp={new_price}"
    )

    resp = await client.post(
        f"{ACTION_BROKER_URL}/submit-intent",
        json={
            "intent":  "update_product_price",
            "payload": {"sku": sku, "new_list_price_gbp": new_price},
        },
        headers=auth_headers,
    )

    if resp.status_code == 403:
        _denied(resp.json().get("detail", {}))
        return False

    resp.raise_for_status()
    result = resp.json()
    _ok(
        f"Intent permitted — audit_id={result.get('audit_id', 'n/a')}  "
        f"target_sor={result.get('target_sor', 'n/a')}"
    )
    _ok(f"Catalogue updated — new list price: £{result.get('list_price_gbp', new_price):.2f}/mo")
    _info(f"CDC fan-out triggered — enriching product metadata for all holders (background)")

    _info(f"Waiting {PROPAGATION_WAIT_S}s for CDC enrichment fan-out…")
    await asyncio.sleep(PROPAGATION_WAIT_S)
    return True


async def step3_verify_billing_unchanged(
    client: httpx.AsyncClient,
    customer_id: str,
    sku: str,
    charge_before: float,
    auth_headers: dict,
) -> float | None:
    """
    Re-query the ACG and confirm the customer's billed charge has NOT changed.
    A catalogue price update only re-enriches product metadata in the cache
    (product_name, list_price_gbp, available_terms); it does not reprice
    contracted billing records.
    Returns the current billed charge for use in step 5.
    """
    _hdr(3, "Verify billing charge unchanged after catalogue update")
    _info(f"GET {ACG_URL}/context/{customer_id}  (billing should be unchanged)")

    resp = await client.get(f"{ACG_URL}/context/{customer_id}", headers=auth_headers)
    resp.raise_for_status()
    ctx = resp.json()

    sub = _find_subscription(ctx, sku)
    charge_after = sub.get("monthly_charge_gbp") if sub else None

    if charge_after is None:
        _warn(f"{sku} not found in context — cannot verify billing isolation")
        return None

    _print_subscription(sub, sku, label="After catalogue update")

    if abs(charge_after - charge_before) < 0.01:
        _ok(
            f"Billed charge unchanged at £{charge_after:.2f}/mo — "
            f"catalogue update correctly isolated to product metadata only"
        )
    else:
        _warn(
            f"Unexpected: charge changed from £{charge_before:.2f} → £{charge_after:.2f} "
            f"without a recalculate_billing intent"
        )

    return charge_after


async def step4_recalculate_billing(
    client: httpx.AsyncClient,
    sku: str,
    auth_headers: dict,
) -> bool:
    """
    Submit recalculate_billing to the Action Broker.
    Returns True on success, False if denied.
    """
    _hdr(4, f"Trigger billing recalculation — {sku}")
    _info(
        f"POST {ACTION_BROKER_URL}/submit-intent  "
        f"intent=recalculate_billing  product_sku={sku}"
    )

    resp = await client.post(
        f"{ACTION_BROKER_URL}/submit-intent",
        json={
            "intent":  "recalculate_billing",
            "payload": {"product_sku": sku},
        },
        headers=auth_headers,
    )

    if resp.status_code == 403:
        _denied(resp.json().get("detail", {}))
        return False

    resp.raise_for_status()
    result = resp.json()
    _ok(
        f"Intent permitted — audit_id={result.get('audit_id', 'n/a')}"
    )
    _ok(
        f"Recalculation complete — "
        f"{result.get('customers_updated', '?')} customer(s) updated, "
        f"{result.get('events_emitted', '?')} CDC event(s) emitted  "
        f"new_list_price_gbp=£{result.get('new_list_price_gbp', '?'):.2f}"
    )

    _info(f"Waiting {PROPAGATION_WAIT_S}s for CDC → cache propagation…")
    await asyncio.sleep(PROPAGATION_WAIT_S)
    return True


async def step5_verify_billing_updated(
    client: httpx.AsyncClient,
    customer_id: str,
    sku: str,
    charge_before: float | None,
    new_list_price: float,
    auth_headers: dict,
) -> None:
    """
    Re-query the ACG and confirm the customer's billed charge has updated to
    reflect the new catalogue list price (accounting for any discount rules that
    may reduce it below the list price).
    """
    _hdr(5, "Verify billing charge updated after recalculation")
    _info(f"GET {ACG_URL}/context/{customer_id}  (billing should now reflect new list price)")

    resp = await client.get(f"{ACG_URL}/context/{customer_id}", headers=auth_headers)
    resp.raise_for_status()
    ctx = resp.json()

    sub = _find_subscription(ctx, sku)
    charge_after = sub.get("monthly_charge_gbp") if sub else None

    if charge_after is None:
        _warn(f"{sku} not found in context — CDC may still be processing")
        return

    _print_subscription(sub, sku, label="After recalculation")

    # ── Validation 1: charge moved from the pre-recalculation value ──────────
    if charge_before is not None:
        delta = charge_after - charge_before
        if abs(delta) > 0.01:
            direction = "increased" if delta > 0 else "decreased"
            _ok(
                f"Charge {direction} as expected: "
                f"£{charge_before:.2f}/mo → £{charge_after:.2f}/mo  "
                f"({'+' if delta > 0 else ''}{delta:.2f}/mo)"
            )
        else:
            _warn(
                f"Charge unchanged at £{charge_after:.2f}/mo after recalculation — "
                f"CDC may still be processing, or new price equals original"
            )

    # ── Validation 2: charge does not exceed the new list price ─────────────
    print(f"\n  {BOLD}Billing validation against updated catalogue price:{RESET}")
    _info(f"Catalogue list price (updated): £{new_list_price:.2f}/mo")
    _info(f"Customer billed amount:         £{charge_after:.2f}/mo")

    discount_pct = (sub or {}).get("total_discount_pct", 0.0)
    if discount_pct and discount_pct > 0.01:
        saving = new_list_price - charge_after
        expected = round(new_list_price * (1 - discount_pct / 100), 2)
        _info(
            f"Discount applied: {discount_pct:.1f}%  "
            f"(saving £{saving:.2f}/mo off list price)"
        )
        if abs(charge_after - expected) < 0.02:
            _ok(
                f"Billed amount correctly derived from new list price "
                f"(£{new_list_price:.2f} × {100 - discount_pct:.1f}% = £{charge_after:.2f}/mo)"
            )
        else:
            _warn(
                f"Billed amount £{charge_after:.2f}/mo does not match expected "
                f"£{expected:.2f}/mo — review discount calculation"
            )
    else:
        if abs(charge_after - new_list_price) < 0.01:
            _ok(
                f"Billed amount matches new catalogue list price: "
                f"£{charge_after:.2f}/mo"
            )
        else:
            _warn(
                f"Billed amount £{charge_after:.2f}/mo does not match catalogue "
                f"list price £{new_list_price:.2f}/mo — CDC may still be processing"
            )

    discount_summary = ctx.get("discount_summary") or {}
    if discount_summary.get("total_discounted_monthly_gbp") is not None:
        _info(
            f"Portfolio total: £{discount_summary['total_discounted_monthly_gbp']:.2f}/mo  "
            f"(saving £{discount_summary.get('total_saving_gbp', 0):.2f}/mo)"
        )


# ===========================================================================
# Entry point
# ===========================================================================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Catalogue price update and billing recalculation demo.\n"
            "Use --client-id to test permission enforcement:\n"
            "  catalogue-admin  (default) — permitted: update_product_price, recalculate_billing\n"
            "  purchase-agent             — denied at step 2 (not in allowed intents)\n"
            "  system-admin               — permitted: full write access\n"
            "  ui-client                  — denied at step 2 (read-only)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "customer_id", nargs="?", default="C001",
        help="Customer to use for billing verification steps (default: C001)",
    )
    p.add_argument(
        "--sku", default="BB-FIBRE-1G",
        help="SKU to update the price for (default: BB-FIBRE-1G)",
    )
    p.add_argument(
        "--new-price", type=float, default=52.00,
        help="New catalogue list price in GBP (default: 52.00)",
    )
    p.add_argument(
        "--client-id", default="catalogue-admin", dest="client_id",
        help="Demo client identity to authenticate as (default: catalogue-admin)",
    )
    return p.parse_args()


async def main() -> None:
    args        = _parse_args()
    customer_id = args.customer_id
    sku         = args.sku
    new_price   = args.new_price
    client_id   = args.client_id

    print(f"\n{BOLD}{'=' * 64}{RESET}")
    print(f"{BOLD}  Action Broker: Pricing Journey{RESET}")
    print(f"{BOLD}{'=' * 64}{RESET}")
    print(f"  Customer:     {BOLD}{customer_id}{RESET}")
    print(f"  SKU:          {BOLD}{sku}{RESET}")
    print(f"  New price:    {BOLD}£{new_price:.2f}/mo{RESET}")
    print(f"  Client ID:    {BOLD}{client_id}{RESET}")
    print(f"  ACG:          {ACG_URL}")
    print(f"  ActionBroker: {ACTION_BROKER_URL}")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # ── Token acquisition ────────────────────────────────────────────────
        try:
            tok_resp = await client.post(
                f"{ACTION_BROKER_URL}/token",
                json={"client_id": client_id},
            )
        except httpx.ConnectError as exc:
            _err(f"Could not connect to Action Broker: {exc}")
            _err("Are all services running?  Run: ./start.sh")
            sys.exit(1)

        if tok_resp.status_code == 400:
            detail = tok_resp.json().get("detail", {})
            _err(
                f"Unknown client_id '{client_id}' — not a registered demo client.\n"
                f"  Registered clients: {detail.get('registered_clients', [])}"
            )
            sys.exit(1)

        tok_resp.raise_for_status()
        access_token = tok_resp.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {access_token}"}
        _ok(f"Token acquired for client_id '{client_id}'")

        # Show write permissions for this client
        try:
            perm_resp = await client.get(
                f"{ACTION_BROKER_URL}/permissions/{client_id}",
                headers=auth_headers,
            )
            if perm_resp.status_code == 200:
                allowed = perm_resp.json().get("allowed_intents", [])
                print(
                    f"  {GREY}Write permissions:{RESET} "
                    f"{', '.join(allowed) if allowed else '(none — read-only)'}"
                )
            elif perm_resp.status_code == 404:
                print(f"  {YELLOW}⚠ client_id '{client_id}' has no write permissions — journey will be denied{RESET}")
        except Exception:
            pass

        # ── Pre-run snapshot ─────────────────────────────────────────────────
        try:
            snapshot = await _snapshot(client, sku)
        except httpx.ConnectError as exc:
            _err(f"Could not connect during snapshot: {exc}")
            sys.exit(1)
        except httpx.HTTPStatusError as exc:
            _err(f"Snapshot failed: HTTP {exc.response.status_code} — {exc.response.text[:200]}")
            sys.exit(1)

        journey_failed = False
        try:
            # 1 — Read current state
            list_price, charge_before = await step1_read_current_state(
                client, customer_id, sku, auth_headers,
            )

            # 2 — Update catalogue price
            if not await step2_update_catalogue_price(client, sku, new_price, auth_headers):
                print(f"\n{BOLD}{YELLOW}Journey ended: intent denied by Action Broker.{RESET}")
                print(f"{GREY}Try --client-id catalogue-admin to see a successful run.{RESET}\n")
                return

            # 3 — Verify billing unchanged
            if charge_before is not None:
                charge_before = await step3_verify_billing_unchanged(
                    client, customer_id, sku, charge_before, auth_headers,
                )
            else:
                _warn(f"Skipping billing isolation check — {customer_id} does not hold {sku}")

            # 4 — Trigger recalculation
            if not await step4_recalculate_billing(client, sku, auth_headers):
                print(f"\n{BOLD}{YELLOW}Journey ended: recalculate_billing denied by Action Broker.{RESET}\n")
                return

            # 5 — Verify billing updated
            await step5_verify_billing_updated(
                client, customer_id, sku, charge_before, new_price, auth_headers,
            )

        except httpx.HTTPStatusError as exc:
            _err(
                f"HTTP {exc.response.status_code} from {exc.request.url}: "
                f"{exc.response.text[:200]}"
            )
            journey_failed = True
        except httpx.ConnectError as exc:
            _err(f"Could not connect: {exc}")
            journey_failed = True
        finally:
            await _teardown(client, sku, snapshot, auth_headers)

    if journey_failed:
        sys.exit(1)

    print(f"\n{BOLD}{GREEN}Journey complete.{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
