"""
purchase_journey.py — Purchase journey demonstration
=====================================================

Walks a customer through a product purchase via the Action Broker.
All write operations are submitted as governed intents — the script never
calls SoR endpoints directly. Reads are served by the ACG and require a
valid JWT Bearer token.

Journey steps:
  1. Query current holdings (ACG read)
  2. Browse compatible products (ACG read)
  3. Submit purchase intent via Action Broker (write — add_subscription)
  4. Validate updated holdings (ACG read)

The --client-id flag selects the demo client identity. A JWT is acquired from
the Action Broker's /token endpoint before the journey starts.

Usage:
  python purchase_journey.py                                      # C001, purchase-agent
  python purchase_journey.py C002                                 # different customer
  python purchase_journey.py --client-id catalogue-admin          # permission denied
  python purchase_journey.py --client-id unknown-bot              # unknown client — token rejected

Requires all services to be running:
  ./start.sh
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

import httpx

# ── Service base URLs ───────────────────────────────────────────────────────
ACG_URL           = "http://localhost:8013"
ACTION_BROKER_URL = "http://localhost:8018"

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
    """Pretty-print an Action Broker permission denial."""
    print(f"\n  {RED}{'─' * 58}{RESET}")
    print(f"  {BOLD}{RED}  Action Broker: Intent Denied{RESET}")
    print(f"  {RED}{'─' * 58}{RESET}")
    print(f"  {RED}caller_id :{RESET}  {detail.get('caller_id', '?')}")
    print(f"  {RED}intent    :{RESET}  {detail.get('intent', '?')}")
    print(f"  {RED}reason    :{RESET}  {detail.get('message', detail.get('error', '?'))}")
    allowed = detail.get("allowed_intents")
    if allowed:
        print(f"  {GREY}allowed   :{RESET}  {', '.join(allowed)}")
    print(f"  {GREY}audit_id  :{RESET}  {detail.get('audit_id', 'n/a')}")
    print(f"  {RED}{'─' * 58}{RESET}")


def _print_subscriptions(subscriptions: list[dict], label: str = "Holdings") -> None:
    print(f"\n  {BOLD}{label}:{RESET}")
    if not subscriptions:
        print(f"    {GREY}(none){RESET}")
        return
    for sub in subscriptions:
        status_colour = GREEN if sub.get("status") == "active" else YELLOW
        charge = sub.get("monthly_charge_gbp", "?")
        charge_str = f"£{charge:.2f}/mo" if isinstance(charge, (int, float)) else str(charge)
        term = sub.get("contract_term_months", "?")
        term_str = f"{term}-mo" if term != 1 else "rolling"
        print(
            f"    {BOLD}{sub.get('product_id', '?')}{RESET}  "
            f"{status_colour}{sub.get('status', '?')}{RESET}  "
            f"{charge_str}  {GREY}({term_str}){RESET}"
        )


def _find_subscription(context: dict, sku: str) -> dict | None:
    subs = (context.get("commercial_state") or {}).get("subscriptions", [])
    return next((s for s in subs if s.get("product_id") == sku), None)


# ===========================================================================
# Setup / teardown
# ===========================================================================

@dataclass
class _Snapshot:
    purchased_sku: str | None = None


def _teardown_hdr(title: str) -> None:
    bar = "─" * 62
    print(f"\n{BOLD}{GREY}{bar}{RESET}")
    print(f"{BOLD}{GREY}  Teardown: {title}{RESET}")
    print(f"{BOLD}{GREY}{bar}{RESET}")


async def _teardown(
    client: httpx.AsyncClient,
    customer_id: str,
    snapshot: _Snapshot,
) -> None:
    """Cancel the purchased subscription via the Action Broker as system-admin."""
    _teardown_hdr("restoring state")

    if snapshot.purchased_sku:
        # Teardown always uses system-admin — acquire a separate token for it
        try:
            token_resp = await client.post(
                f"{ACTION_BROKER_URL}/token",
                json={"client_id": "system-admin"},
            )
            token_resp.raise_for_status()
            sysadmin_token = token_resp.json()["access_token"]
            sysadmin_headers = {"Authorization": f"Bearer {sysadmin_token}"}
        except Exception as exc:
            _warn(f"Could not acquire system-admin token for teardown: {exc}")
            sysadmin_headers = {}

        try:
            resp = await client.post(
                f"{ACTION_BROKER_URL}/submit-intent",
                json={
                    "intent":      "cancel_subscription",
                    "customer_id": customer_id,
                    "payload": {
                        "product_sku":   snapshot.purchased_sku,
                        "reason_code":   "customer_request",
                        "reason_detail": "demo teardown",
                    },
                },
                headers=sysadmin_headers,
            )
            resp.raise_for_status()
            _ok(f"Cancelled {snapshot.purchased_sku} on {customer_id} via Action Broker")
        except Exception as exc:
            _warn(f"Could not cancel {snapshot.purchased_sku}: {exc}")

        _info(f"Waiting {PROPAGATION_WAIT_S}s for CDC propagation…")
        await asyncio.sleep(PROPAGATION_WAIT_S)

    _ok("State restored — safe to run again")


# ===========================================================================
# Journey steps
# ===========================================================================

async def step1_query_holdings(
    client: httpx.AsyncClient,
    customer_id: str,
    auth_headers: dict,
) -> dict:
    """Query the current customer context from the ACG."""
    _hdr(1, "Query current customer holdings")
    _info(f"GET {ACG_URL}/context/{customer_id}")

    resp = await client.get(f"{ACG_URL}/context/{customer_id}", headers=auth_headers)
    resp.raise_for_status()
    ctx = resp.json()

    _ok(
        f"Customer {ctx.get('customer_id')}  "
        f"assembly_state={ctx.get('assembly_state')}  "
        f"retrieval_source={ctx.get('retrieval_source')}"
    )
    profile = ctx.get("profile") or {}
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    if name:
        _info(f"Name: {name}")

    subs = (ctx.get("commercial_state") or {}).get("subscriptions", [])
    _print_subscriptions(subs)

    discount = ctx.get("discount_summary") or {}
    if discount.get("policies_applied"):
        _info(f"Discount policies active: {', '.join(discount['policies_applied'])}")
        _info(
            f"Total billed: £{discount.get('total_discounted_monthly_gbp', '?'):.2f}/mo  "
            f"(saving £{discount.get('total_saving_gbp', 0):.2f}/mo)"
        )

    return ctx


async def step2_browse_compatible(
    client: httpx.AsyncClient,
    customer_id: str,
    auth_headers: dict,
) -> dict:
    """Fetch compatible products for the customer via the ACG."""
    _hdr(2, "Browse compatible products")
    _info(f"GET {ACG_URL}/compatible-offers/{customer_id}")

    resp = await client.get(
        f"{ACG_URL}/compatible-offers/{customer_id}", headers=auth_headers
    )
    resp.raise_for_status()
    offers = resp.json()

    held     = offers.get("held_skus", [])
    products = offers.get("compatible_products", [])

    _ok(f"Currently holding: {', '.join(held)}")
    _ok(f"{len(products)} compatible product(s) found")

    if not products:
        _warn("No compatible products — cannot proceed with purchase demo")
        return offers

    print(f"\n  {BOLD}Compatible products:{RESET}")
    for p in products:
        via_str = ", ".join(
            f"{v['source_sku']} ({v['relation']})" for v in p.get("compatible_via", [])
        )
        print(
            f"    {BOLD}{p['sku']}{RESET}  {p.get('product_name', '')}  "
            f"£{p.get('list_price_gbp', '?'):.2f}/mo  "
            f"{GREY}via {via_str}{RESET}"
        )
        best = min(
            p.get("proposals", [{}]),
            key=lambda x: x.get("delta", {}).get("monthly_charge_delta_gbp", 0),
        )
        if best.get("delta", {}).get("net_change") == "saving":
            print(
                f"      {GREEN}→ with {best['contract_term_months']}-mo contract: "
                f"saves £{abs(best['delta']['saving_delta_gbp']):.2f}/mo through bundle discount{RESET}"
            )

    return offers


async def step3_purchase_via_broker(
    client: httpx.AsyncClient,
    customer_id: str,
    client_id: str,
    offers: dict,
    auth_headers: dict,
) -> tuple[str, float, int] | None:
    """
    Submit a purchase intent to the Action Broker.

    Returns (chosen_sku, list_price, contract_term_months) on success, or None
    if the intent is denied. A denial is printed but does not raise — the caller
    can inspect the returned None to decide whether to abort the journey.
    """
    products = offers.get("compatible_products", [])
    if not products:
        raise RuntimeError("No compatible products available to purchase")

    chosen      = products[0]
    chosen_sku  = chosen["sku"]
    list_price  = chosen.get("list_price_gbp", 0.0)
    terms       = chosen.get("available_terms_months", [1])
    chosen_term = min(terms)

    _hdr(3, f"Submit purchase intent — {chosen_sku}")
    _info(f"client_id: {BOLD}{client_id}{RESET}")
    _info(
        f"POST {ACTION_BROKER_URL}/submit-intent  "
        f"intent=add_subscription  sku={chosen_sku}  term={chosen_term}-mo"
    )

    # caller_id is derived from the JWT Bearer token — not sent in the body
    resp = await client.post(
        f"{ACTION_BROKER_URL}/submit-intent",
        json={
            "intent":      "add_subscription",
            "customer_id": customer_id,
            "payload": {
                "sku":                  chosen_sku,
                "contract_term_months": chosen_term,
            },
        },
        headers=auth_headers,
    )

    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        _denied(detail)
        return None

    resp.raise_for_status()
    result = resp.json()

    _ok(
        f"Intent permitted — audit_id={result.get('audit_id', 'n/a')}  "
        f"target_sor={result.get('target_sor', 'n/a')}"
    )
    _ok(
        f"Billing written — assembly_status={result.get('assembly_status')}  "
        f"correlation_id={result.get('correlation_id', 'n/a')}"
    )
    _info(f"Waiting {PROPAGATION_WAIT_S}s for CDC propagation…")
    await asyncio.sleep(PROPAGATION_WAIT_S)

    return chosen_sku, list_price, chosen_term


async def step4_validate_purchase(
    client: httpx.AsyncClient,
    customer_id: str,
    purchased_sku: str,
    auth_headers: dict,
) -> dict:
    """Re-query the ACG to confirm the purchased SKU appears in holdings."""
    _hdr(4, "Validate purchase — re-query customer holdings")
    _info(f"GET {ACG_URL}/context/{customer_id}")

    resp = await client.get(f"{ACG_URL}/context/{customer_id}", headers=auth_headers)
    resp.raise_for_status()
    ctx = resp.json()

    subs = (ctx.get("commercial_state") or {}).get("subscriptions", [])
    _print_subscriptions(subs, label="Updated holdings")

    new_sub = _find_subscription(ctx, purchased_sku)
    if new_sub:
        _ok(
            f"{purchased_sku} confirmed in holdings — "
            f"£{new_sub.get('monthly_charge_gbp', '?'):.2f}/mo  "
            f"status={new_sub.get('status', '?')}"
        )
    else:
        _warn(f"{purchased_sku} not yet visible — CDC may still be processing")

    discount = ctx.get("discount_summary") or {}
    if discount.get("policies_applied"):
        _info(
            f"Portfolio total: £{discount.get('total_discounted_monthly_gbp', '?'):.2f}/mo  "
            f"(saving £{discount.get('total_saving_gbp', 0):.2f}/mo)"
        )

    return ctx


# ===========================================================================
# Entry point
# ===========================================================================

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Purchase journey demo — submits write intents through the Action Broker.\n"
            "Use --client-id to test permission enforcement:\n"
            "  purchase-agent  (default) — permitted: add_subscription\n"
            "  catalogue-admin           — denied: not allowed to add subscriptions\n"
            "  system-admin              — permitted: full write access\n"
            "  <any other value>         — rejected: unknown client, no token issued"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "customer_id", nargs="?", default="C001",
        help="Customer ID to use for the journey (default: C001)",
    )
    p.add_argument(
        "--client-id", default="purchase-agent",
        dest="client_id",
        help="Demo client identity to authenticate as (default: purchase-agent)",
    )
    return p.parse_args()


async def main() -> None:
    args        = _parse_args()
    customer_id = args.customer_id
    client_id   = args.client_id

    print(f"\n{BOLD}{'=' * 64}{RESET}")
    print(f"{BOLD}  Action Broker: Purchase Journey{RESET}")
    print(f"{BOLD}{'=' * 64}{RESET}")
    print(f"  Customer:     {BOLD}{customer_id}{RESET}")
    print(f"  Client ID:    {BOLD}{client_id}{RESET}")
    print(f"  ACG:          {ACG_URL}")
    print(f"  ActionBroker: {ACTION_BROKER_URL}")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # ── Acquire JWT from the Action Broker /token endpoint ────────────────
        try:
            token_resp = await client.post(
                f"{ACTION_BROKER_URL}/token",
                json={"client_id": client_id},
            )
        except httpx.ConnectError as exc:
            _err(f"Could not connect to Action Broker: {exc}")
            _err("Are all services running?  Run: ./start.sh")
            sys.exit(1)

        if token_resp.status_code == 400:
            detail = token_resp.json().get("detail", {})
            _err(f"Unknown client_id '{client_id}' — token not issued")
            registered = detail.get("registered_clients", [])
            if registered:
                _err(f"Registered clients: {', '.join(registered)}")
            sys.exit(1)

        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]
        auth_headers = {"Authorization": f"Bearer {access_token}"}
        _ok(f"Token acquired for client_id '{client_id}'")

        # Display caller's write permissions (informational)
        try:
            perm_resp = await client.get(
                f"{ACTION_BROKER_URL}/permissions/{client_id}",
                headers=auth_headers,
            )
            if perm_resp.status_code == 200:
                perm = perm_resp.json()
                print(
                    f"\n  {GREY}Caller permissions:{RESET} "
                    f"{', '.join(perm.get('allowed_intents', [])) or '(none)'}"
                )
            elif perm_resp.status_code == 404:
                print(
                    f"\n  {YELLOW}⚠ client_id '{client_id}' has no write permissions "
                    f"— purchase will be denied{RESET}"
                )
        except Exception:
            pass

        snapshot       = _Snapshot()
        journey_failed = False

        try:
            # 1 — Current holdings
            await step1_query_holdings(client, customer_id, auth_headers)

            # 2 — Browse compatible offers
            offers = await step2_browse_compatible(client, customer_id, auth_headers)

            if not offers.get("compatible_products"):
                _warn("Skipping purchase — no compatible products returned")
                return

            # 3 — Submit purchase via Action Broker with the acquired JWT
            result = await step3_purchase_via_broker(
                client, customer_id, client_id, offers, auth_headers
            )

            if result is None:
                # Permission denied — nothing was written; no teardown needed
                print(f"\n{BOLD}{YELLOW}Journey ended: intent denied by Action Broker.{RESET}")
                print(
                    f"{GREY}Try --client-id purchase-agent to see a successful purchase.{RESET}\n"
                )
                return

            purchased_sku, _, _ = result
            snapshot.purchased_sku = purchased_sku

            # 4 — Validate purchase in holdings
            await step4_validate_purchase(client, customer_id, purchased_sku, auth_headers)

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
            await _teardown(client, customer_id, snapshot)

    if journey_failed:
        sys.exit(1)

    print(f"\n{BOLD}{GREEN}Journey complete.{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
