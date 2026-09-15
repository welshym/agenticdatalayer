"""
auth.py — JWT utilities for the enterprise architecture demo
===================================================

Uses HS256 with a shared secret. NOT for production use — a real deployment
would use asymmetric keys (RS256/ES256) issued by an authorisation server.

All services import verify_token() to validate incoming Bearer tokens.
The POST /token endpoint on the Action Broker issues tokens for known client IDs.
Demo scripts and tests acquire tokens from that endpoint rather than calling
mint_token() directly, so the flow mirrors the real client_credentials grant.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import jwt

# Shared signing secret — demo only, never commit a real secret here
DEMO_SECRET    = "demo-secret-not-for-production"
ALGORITHM      = "HS256"
ISSUER         = "enterprise-arch-demo"
DEFAULT_EXPIRY = 3600  # seconds


# Registry of client IDs that may be issued tokens.
# Separate from OPA policies/data.json — this governs who can authenticate;
# OPA governs what authenticated write intents are permitted.
DEMO_CLIENTS: dict[str, str] = {
    "purchase-agent":  "Customer-facing purchase agent. Reads customer context; may add and cancel subscriptions.",
    "catalogue-admin": "Product catalogue administrator. Reads all data; may update product prices and trigger billing recalculations.",
    "system-admin":    "System administrator. Full read and write access across all SoRs.",
    "ui-client":       "Agent UI. Read-only access to customer context and compatible offers.",
}


def mint_token(client_id: str, expires_in: int = DEFAULT_EXPIRY) -> str:
    """
    Mint a signed HS256 JWT for the given client_id.
    Raises ValueError if client_id is not in DEMO_CLIENTS.
    """
    if client_id not in DEMO_CLIENTS:
        raise ValueError(
            f"Unknown client_id '{client_id}'. "
            f"Registered clients: {list(DEMO_CLIENTS)}"
        )
    now = datetime.now(timezone.utc)
    payload = {
        "sub": client_id,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
    }
    return jwt.encode(payload, DEMO_SECRET, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    """
    Verify and decode a JWT. Returns the decoded payload dict.

    Raises:
      jwt.ExpiredSignatureError  — token has expired
      jwt.InvalidIssuerError     — wrong issuer
      jwt.InvalidTokenError      — any other validation failure
    """
    return jwt.decode(
        token,
        DEMO_SECRET,
        algorithms=[ALGORITHM],
        options={"require": ["sub", "exp", "iss", "iat"]},
        issuer=ISSUER,
    )
