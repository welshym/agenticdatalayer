"""
Logging Service — cross-cutting concern
========================================
Port: 8015

In-memory structured log store. All services emit log entries here
with explicit action + reason fields, creating a full audit trail of
every assembly decision.

The log is the primary debugging surface for the demo: it shows exactly
WHY each decision was made (join_pending, join_completed, timeout_triggered,
etc.) and ties all events for one assembly run together via correlation_id.

Routes:
  POST /logs                          — append a log entry
  GET  /logs                          — all entries (filters: customer_id, service, limit)
  GET  /logs/trace/{correlation_id}   — all entries for one assembly run
  GET  /logs/customer/{customer_id}   — all entries for a customer (newest first)
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Capped at 500 — oldest entries drop off automatically
_LOG: deque[dict] = deque(maxlen=500)
_lock = asyncio.Lock()


app = FastAPI(
    title="Logging Service",
    description="Structured assembly log — cross-cutting concern",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class LogEntry(BaseModel):
    service: str
    action: str
    reason: str
    customer_id: str | None = None
    event_id: str | None = None
    correlation_id: str | None = None
    outcome: str = "success"    # "success" | "partial" | "timeout" | "error"
    details: dict[str, Any] = {}


@app.post("/logs", summary="Append a structured log entry")
async def append_log(entry: LogEntry) -> dict:
    record = {
        "log_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **entry.model_dump(),
    }
    async with _lock:
        _LOG.appendleft(record)
    return record


@app.get("/logs", summary="Query log entries with optional filters")
async def get_logs(
    customer_id: str | None = Query(None),
    service:     str | None = Query(None),
    limit:       int        = Query(100),
) -> list[dict]:
    async with _lock:
        entries = list(_LOG)
    if customer_id:
        entries = [e for e in entries if e.get("customer_id") == customer_id]
    if service:
        entries = [e for e in entries if e.get("service") == service]
    return entries[:limit]


@app.get("/logs/trace/{correlation_id}", summary="Full trace for one assembly run")
async def get_trace(correlation_id: str) -> list[dict]:
    async with _lock:
        return [e for e in _LOG if e.get("correlation_id") == correlation_id]


@app.get("/logs/customer/{customer_id}", summary="All log entries for a customer (newest first)")
async def get_customer_logs(customer_id: str, limit: int = Query(50)) -> list[dict]:
    async with _lock:
        entries = [e for e in _LOG if e.get("customer_id") == customer_id]
    return entries[:limit]


if __name__ == "__main__":
    uvicorn.run("log_app:app", host="0.0.0.0", port=8015, reload=True)
