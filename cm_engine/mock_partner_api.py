"""Stand-in for https://api.partner.example so the demo runs with no internet.

The deliberate ~400ms latency is the point: it makes the cache-HIT contrast
something the audience sees rather than something the presenter asserts.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, HTTPException

from cm_engine.config import MOCK_API_PORT

app = FastAPI(title="Mock Partner Orders API")

UPSTREAM_LATENCY_SECONDS = 0.4

_STATUSES = ("pending", "shipped", "delivered", "cancelled")
_CARRIERS = ("Aramex", "SMSA", "DHL", "Naqel")

# Orders cancelled during this process's lifetime, so the propose-apply demo
# shows a real state change.
_cancelled: set[str] = set()


def _deterministic(order_id: str) -> tuple[str, str, str]:
    """Same order ID always yields the same status -- a cache that returned a
    different answer each call would be indistinguishable from a broken one."""
    digest = hashlib.sha256(order_id.encode()).digest()
    status = _STATUSES[digest[0] % len(_STATUSES)]
    carrier = _CARRIERS[digest[1] % len(_CARRIERS)]
    eta = (datetime.now(timezone.utc) + timedelta(days=1 + digest[2] % 5)).isoformat(
        timespec="seconds"
    )
    return status, carrier, eta


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "mock-partner-api"}


@app.get("/v1/orders/{order_id}/status")
async def order_status(order_id: str) -> dict:
    await asyncio.sleep(UPSTREAM_LATENCY_SECONDS)

    if not order_id.startswith("ORD-"):
        raise HTTPException(status_code=404, detail=f"no such order: {order_id}")

    if order_id in _cancelled:
        return {"orderId": order_id, "status": "cancelled", "eta": "", "carrier": ""}

    status, carrier, eta = _deterministic(order_id)
    return {"orderId": order_id, "status": status, "eta": eta, "carrier": carrier}


@app.post("/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: str, body: dict | None = None) -> dict:
    await asyncio.sleep(UPSTREAM_LATENCY_SECONDS)

    if not order_id.startswith("ORD-"):
        raise HTTPException(status_code=404, detail=f"no such order: {order_id}")

    status, _, _ = _deterministic(order_id)
    if status in {"delivered", "shipped"} and order_id not in _cancelled:
        raise HTTPException(
            status_code=409, detail=f"order {order_id} has already {status}; cannot cancel"
        )

    _cancelled.add(order_id)
    return {
        "orderId": order_id,
        "status": "cancelled",
        "cancelledAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=MOCK_API_PORT, log_level="warning")


if __name__ == "__main__":
    main()
