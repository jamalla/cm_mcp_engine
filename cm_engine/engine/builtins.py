"""Pure functions behind `binding.type: "none"`.

These are the offline half of the demo: real tools that answer with no API,
no network, and no secrets. A contract reaches one through a
`builtin://<name>` handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Business days by zone and speed. Deliberately boring data -- the interesting
# part is that a contract, not code, decided this tool exists.
_ZONE_DAYS = {
    "domestic": {"standard": (2, 4), "express": (1, 2), "overnight": (1, 1)},
    "regional": {"standard": (4, 7), "express": (2, 4), "overnight": (1, 2)},
    "international": {"standard": (8, 16), "express": (4, 8), "overnight": (2, 4)},
}


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


# Keyed by the contract's `builtin://<name>` handler string.
BUILTINS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "builtin://estimate_delivery_window": estimate_delivery_window,
}
