"""Contract surface -> A2UI messages.

A2UI (v0.9.1) splits a rendered interface in two: `updateComponents` carries the
component tree, `updateDataModel` carries the values it binds to. That split is
the one this platform already has. The tree comes from the contract, where a
human reviewed it; the data comes from the call, which nobody saw in advance.
Neither half is written by the agent -- an LLM authoring the interface is what
the registry exists to prevent -- so this module is a translation, never a
generation.

It also buys the thing A2UI was designed for. The tree is known the moment the
contract is selected, before any request goes out, so it is sent then: the shape
of the answer is on screen while the sandbox is still working, and the result
fills it in when it lands.
"""

from __future__ import annotations

from typing import Any

# Pinned. v0.9.1 is A2UI's current production release and its specification
# directory is closed, so a contract written against it cannot be invalidated by
# the spec moving underneath it. The version rides on every message because the
# client is entitled to refuse one it does not implement.
VERSION = "v0.9.1"
DEFAULT_CATALOG = "a2ui.org:basic"


def surface_of(entry) -> dict | None:
    """The contract's declared surface, or None when it did not declare one.

    A tool without a surface is not an error: raw JSON is the honest rendering
    for some results, and the client falls back to it.
    """
    surface = (entry.interface.get("response") or {}).get("ui")
    return surface if isinstance(surface, dict) and surface.get("components") else None


def _component(spec: dict) -> dict:
    """One component, with the contract's own annotations removed.

    `_comment` keys are how a contract explains itself to a reviewer. A2UI's
    catalog sets `unevaluatedProperties: false`, so leaving one in would make
    the message invalid against the very schema the client validates with.
    """
    return {key: value for key, value in spec.items() if not key.startswith("_")}


def structure_messages(entry, surface_id: str) -> list[dict[str, Any]]:
    """createSurface + updateComponents: everything knowable from the contract."""
    surface = surface_of(entry)
    if surface is None:
        return []

    return [
        {
            "version": VERSION,
            "createSurface": {
                "surfaceId": surface_id,
                "catalogId": surface.get("catalogId", DEFAULT_CATALOG),
            },
        },
        {
            "version": VERSION,
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [_component(c) for c in surface["components"]],
            },
        },
    ]


def data_messages(entry, surface_id: str, output: Any) -> list[dict[str, Any]]:
    """updateDataModel: the result, placed at the root of the surface's data.

    The whole payload goes in unchanged, which is what makes the contract's
    pointers mean what they say -- `/items` for a collection, `/name` for a
    detail record, and relative paths inside a row template resolving against
    the item the client is currently instantiating.
    """
    if surface_of(entry) is None:
        return []

    return [
        {
            "version": VERSION,
            "updateDataModel": {"surfaceId": surface_id, "value": output},
        }
    ]
