"""Offline stand-in for a configured upstream, speaking the Salla Admin API's shape.

The engine's upstream table points `salla` here whenever DEV_OFFLINE is set, so
the generated code is byte-identical online and off -- same envelope, same
pagination object, same error shape. Only the host differs.

Two deliberate behaviors:

* every call sleeps ~400ms, which is what makes the cache-HIT contrast something
  the audience sees rather than something the presenter asserts;
* `keyword=boom` fails with a 503. One fault injection, so the failure path the
  contracts declare is exercised by a test instead of merely asserted.

Any token is accepted. Authorization is the real upstream's job; refusing the
placeholder token here would only break the demo for anyone without a store.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cm_engine.config import MOCK_API_PORT

app = FastAPI(title="Mock Salla Admin API")

UPSTREAM_LATENCY_SECONDS = 0.4
PER_PAGE = 5

_CATEGORY_NAMES = (
    "Shoes",
    "Sneakers",
    "Sandals",
    "Bags",
    "Backpacks",
    "Accessories",
    "Watches",
    "Sunglasses",
    "Clearance",
)

# Parent ids give the tree a shape: 0 is top level.
_PARENTS = {2: 1, 3: 1, 5: 4, 7: 6, 8: 6}


def _category(index: int) -> dict:
    """One category record, deterministic so a cached answer is comparable.

    Carries more fields than any contract exposes -- `urls` and `updated_at`
    exist precisely so a test can prove the generated code projects the response
    down to what the contract promised.
    """
    number = index + 1
    name = _CATEGORY_NAMES[index]
    return {
        "id": 1000 + number,
        "name": name,
        "parent_id": 1000 + _PARENTS[number] if number in _PARENTS else 0,
        "status": "hidden" if number % 4 == 0 else "active",
        "sort_order": number,
        "image": None if number % 3 == 0 else f"https://cdn.salla.example/{name.lower()}.png",
        "urls": {"customer": f"https://demo.salla.sa/{name.lower()}"},
        "updated_at": "2026-07-01 09:00:00",
    }


_CATEGORIES = [_category(i) for i in range(len(_CATEGORY_NAMES))]

# Categories deleted during this process's lifetime, so the propose-apply demo
# shows a real state change.
_deleted: set[int] = set()


def _live() -> list[dict]:
    return [c for c in _CATEGORIES if c["id"] not in _deleted]


def _envelope(data, *, status: int = 200, pagination: dict | None = None) -> JSONResponse:
    payload: dict = {"status": status, "success": True, "data": data}
    if pagination is not None:
        # Beside the data, not inside it -- which is what the contract's
        # `pagination: standard` tells the engine to expect.
        payload["pagination"] = pagination
    return JSONResponse(payload, status_code=status)


def _failure(status: int, code: str, message: str, fields: dict | None = None) -> JSONResponse:
    error: dict = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return JSONResponse({"status": status, "success": False, "error": error}, status_code=status)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "mock-salla-admin-api"}


@app.get("/admin/v2/categories")
async def list_categories(
    page: int = 1, keyword: str | None = None, status: str | None = None
) -> JSONResponse:
    await asyncio.sleep(UPSTREAM_LATENCY_SECONDS)

    if keyword == "boom":
        return _failure(503, "service_unavailable", "The categories service is briefly unavailable.")

    rows = _live()
    if keyword:
        rows = [c for c in rows if keyword.lower() in c["name"].lower()]
    if status:
        rows = [c for c in rows if c["status"] == status]

    total = len(rows)
    total_pages = max(1, -(-total // PER_PAGE))
    start = (max(page, 1) - 1) * PER_PAGE
    window = rows[start : start + PER_PAGE]

    return _envelope(
        window,
        pagination={
            "count": len(window),
            "total": total,
            "perPage": PER_PAGE,
            "currentPage": page,
            "totalPages": total_pages,
            # Salla really sends this: a prebuilt URL carrying the connected app's
            # id. Kept here so a test can prove the engine does not pass it on.
            "links": {
                "next": f"http://api.salla.example/admin/v2/categories"
                f"?connected_app_id=1642267012&page={page + 1}"
            },
        },
    )


@app.post("/admin/v2/categories")
async def create_category(request: Request) -> JSONResponse:
    await asyncio.sleep(UPSTREAM_LATENCY_SECONDS)
    body = await request.json() if await request.body() else {}

    name = str(body.get("name") or "")
    if len(name) < 3:
        # Salla's 422 shape: per-field messages under error.fields.
        return _failure(
            422,
            "validation_error",
            "The given data was invalid.",
            fields={"name": ["The name must be at least 3 characters."]},
        )

    created = {
        "id": 2000 + len(_CATEGORIES) + 1,
        "name": name,
        "parent_id": body.get("parent_id", 0),
        "status": body.get("status", "active"),
        "sort_order": len(_CATEGORIES) + 1,
        "image": None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return _envelope(created, status=201)


@app.delete("/admin/v2/categories/{category_id}")
async def delete_category(category_id: int) -> JSONResponse:
    await asyncio.sleep(UPSTREAM_LATENCY_SECONDS)

    known = {c["id"] for c in _CATEGORIES}
    if category_id not in known or category_id in _deleted:
        return _failure(404, "not_found", f"No category exists with the id {category_id}.")

    _deleted.add(category_id)
    return _envelope({"id": category_id, "deleted": True})


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=MOCK_API_PORT, log_level="warning")


if __name__ == "__main__":
    main()
