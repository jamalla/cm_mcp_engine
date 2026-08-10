"""Pure functions behind `binding.type: "none"`.

These are the offline half of the demo: real tools that answer with no API,
no network, and no secrets. A contract reaches one through a
`builtin://<name>` handler.
"""

from __future__ import annotations

from typing import Any, Callable

# Business days by zone and speed. Deliberately boring data -- the interesting
# part is that a contract, not code, decided this tool exists.
_ZONE_DAYS = {
    "domestic": {"standard": (2, 4), "express": (1, 2), "overnight": (1, 1)},
    "regional": {"standard": (4, 7), "express": (2, 4), "overnight": (1, 2)},
    "international": {"standard": (8, 16), "express": (4, 8), "overnight": (2, 4)},
}

_COUNTRY_ZONES = {
    "SA": "domestic",
    "AE": "regional",
    "KW": "regional",
    "BH": "regional",
    "QA": "regional",
    "OM": "regional",
    "EG": "regional",
    "JO": "regional",
}

_RETURN_WINDOWS = {"standard": 30, "electronics": 14, "final_sale": 0}


def estimate_delivery_window(args: dict[str, Any]) -> dict[str, Any]:
    zone = str(args["zone"]).lower()
    speed = str(args.get("speed") or "standard").lower()

    by_speed = _ZONE_DAYS[zone]
    min_days, max_days = by_speed.get(speed, by_speed["standard"])

    if min_days == max_days:
        summary = f"About {min_days} business day{'s' if min_days != 1 else ''}."
    else:
        summary = f"About {min_days}-{max_days} business days."

    return {
        "zone": zone,
        "speed": speed,
        "minDays": min_days,
        "maxDays": max_days,
        "summary": summary,
    }


def lookup_shipping_zone(args: dict[str, Any]) -> dict[str, Any]:
    code = str(args["countryCode"]).upper()
    return {"countryCode": code, "zone": _COUNTRY_ZONES.get(code, "international")}


def check_return_window(args: dict[str, Any]) -> dict[str, Any]:
    days = int(args["daysSinceDelivery"])
    category = str(args.get("category") or "standard").lower()
    window = _RETURN_WINDOWS.get(category, _RETURN_WINDOWS["standard"])
    remaining = window - days
    eligible = remaining > 0

    if window == 0:
        summary = "Final sale items cannot be returned."
    elif eligible:
        summary = f"Eligible -- {remaining} day{'s' if remaining != 1 else ''} left to return."
    else:
        summary = f"The {window}-day return window closed {abs(remaining)} day(s) ago."

    return {
        "eligible": eligible,
        "windowDays": window,
        "daysRemaining": max(remaining, 0),
        "summary": summary,
    }


# Keyed by the contract's `builtin://<name>` handler string.
BUILTINS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "builtin://estimate_delivery_window": estimate_delivery_window,
    "builtin://lookup_shipping_zone": lookup_shipping_zone,
    "builtin://check_return_window": check_return_window,
}
