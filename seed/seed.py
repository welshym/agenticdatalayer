"""
Verification script — confirms the cache and permanent store are fully populated.

The CRM and Billing SoRs emit events for all seed records at startup, so the
cache should be pre-populated by the time services are ready. This script
verifies that all expected customers are present and assembled as 'complete'.

Run manually after ./start.sh to confirm the startup event pipeline worked:
  python seed.py
"""

from __future__ import annotations

import sys
import time

import httpx

CRM_URL   = "http://localhost:8010"
CACHE_URL = "http://localhost:8012"


def wait_for(url: str, label: str, retries: int = 20, delay: float = 0.5) -> bool:
    for attempt in range(1, retries + 1):
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        print(f"  Waiting for {label} ({attempt}/{retries})…", flush=True)
        time.sleep(delay)
    print(f"ERROR: {label} not responding at {url}", file=sys.stderr)
    return False


def main() -> None:
    print("Verifying cache population…\n")

    if not wait_for(f"{CRM_URL}/customers", "CRM SoR"):
        sys.exit(1)
    if not wait_for(f"{CACHE_URL}/status", "Context Cache"):
        sys.exit(1)

    with httpx.Client(timeout=10.0) as client:
        crm_customers = client.get(f"{CRM_URL}/customers").json()
        expected_ids  = {c["customer_id"] for c in crm_customers}

        status   = client.get(f"{CACHE_URL}/status").json()
        hot      = set(status["hot_cache"]["customer_ids"])
        perm     = set(status["permanent_store"]["customer_ids"])
        perm_all = client.get(f"{CACHE_URL}/permanent").json()

        print(f"  Expected customers : {sorted(expected_ids)}")
        print(f"  Hot cache          : {sorted(hot)}")
        print(f"  Permanent store    : {sorted(perm)}")

        missing_hot  = expected_ids - hot
        missing_perm = expected_ids - perm

        if missing_hot:
            print(f"\n  WARNING — not in hot cache  : {sorted(missing_hot)}")
        if missing_perm:
            print(f"\n  ERROR   — not in permanent  : {sorted(missing_perm)}")
            sys.exit(1)

        print("\n  Assembly states:")
        all_complete = True
        for cid in sorted(expected_ids):
            record = perm_all.get(cid, {})
            state  = record.get("assembly_state", "unknown")
            marker = "✓" if state == "complete" else "✗"
            print(f"    {marker} {cid}  {state}")
            if state != "complete":
                all_complete = False

        if all_complete:
            print(f"\nAll {len(expected_ids)} customers assembled as complete.\n")
        else:
            print("\nWARNING — some customers not fully assembled.\n")
            sys.exit(1)


if __name__ == "__main__":
    main()
