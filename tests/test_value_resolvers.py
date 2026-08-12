"""A readable value in, this store's id out -- inside the generated code.

Salla's order filter is keyed on ids that exist only inside one merchant's
store: `shipped` is 1201821018 here and a different number next door. Exposing
that as the tool's argument pushes an upstream detail into the agent's
vocabulary and gets the wrong answer every time the agent has not just looked it
up -- and Salla answers a filter it cannot use by ignoring it and returning
everything, so the wrong answer arrives looking like the right one.

So the tool takes the name a person would say and the generated code does the
translation, beside the other things the caller never sees: the host, the token,
the array serialization.
"""

from __future__ import annotations

import copy
import types

import pytest

from cm_engine.engine import codemode
from cm_engine.registry.loader import (
    SUPPORTED_SCHEMA_IDS,
    ToolEntry,
    unsupported_schema,
)

RESOLVER = {
    "contract": "list_order_statuses",
    "path": "/orders/statuses",
    "dataPath": "data",
    "matchOn": ["slug", "name", "translations.en.name"],
    "sendField": "id",
    "onMiss": "error",
}

CONTRACT = {
    "contractVersion": "2.0.0",
    "kind": "single-tool",
    "interface": {
        "name": "list_orders",
        "description": "Lists the store's orders.",
        "whenToUse": ["The merchant wants recent orders."],
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        "input": {
            "schema": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "status": {"type": "array", "items": {"type": "string"}},
                },
            }
        },
        "response": {"schema": {"type": "object", "properties": {"id": {"type": "integer"}}}},
    },
    "binding": {
        "type": "http",
        "http": {
            "method": "GET",
            "path": "/orders",
            "auth": {"scopes": ["orders.read"]},
            "parameters": {
                "query": [
                    {"name": "page"},
                    {"name": "status", "style": "bracket", "resolve": RESOLVER},
                ]
            },
            "response": {
                "dataPath": "data",
                "collection": True,
                "pagination": "standard",
                "successStatuses": [200],
            },
        },
    },
    "governance": {
        "execution": {"mode": "direct", "humanApproval": "never"},
        "caching": {"cacheable": True, "keyBy": ["page", "status"], "ttlSeconds": 60},
    },
}

# The store's real statuses, trimmed. Keeps the pair that must not be confused
# (delivered / delivering) and a name whose English lives under translations.
RECORDS = [
    {"id": 1201821018, "name": "تم الشحن", "slug": "shipped",
     "translations": {"en": {"name": "Shipped"}}},
    {"id": 1975858777, "name": "تم التوصيل", "slug": "delivered",
     "translations": {"en": {"name": "Delivered"}}},
    {"id": 469243736, "name": "جاري التوصيل", "slug": "delivering",
     "translations": {"en": {"name": "Out for delivery"}}},
]


def _entry(contract: dict) -> ToolEntry:
    return ToolEntry(
        name=contract["interface"]["name"],
        version=contract["contractVersion"],
        interface=contract["interface"],
        binding=contract["binding"],
        governance=contract["governance"],
        validation=contract.get("validation", {}),
        raw=contract,
    )


@pytest.fixture
def generated():
    """The real generated module, with the network stubbed out."""
    source = codemode.generate(_entry(copy.deepcopy(CONTRACT)))
    module = types.ModuleType("generated_list_orders")
    exec(compile(source, "generated_list_orders.py", "exec"), module.__dict__)  # noqa: S102
    module.fetch_records = lambda resolver: RECORDS
    return module


# -- resolution ------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["shipped"], [1201821018]),          # the canonical slug
        (["Delivered"], [1975858777]),        # the English translation
        (["تم الشحن"], [1201821018]),          # the store's own label
        (["SHIPPED"], [1201821018]),          # case is not a distinction
        (["shipped", "delivered"], [1201821018, 1975858777]),
    ],
)
def test_a_readable_value_becomes_this_store_s_id(generated, given, expected):
    assert generated.resolve_values({"status": given})["status"] == expected


def test_delivered_does_not_also_match_delivering(generated):
    """Equality, not containment. One request must not widen into two states."""
    assert generated.resolve_values({"status": ["delivered"]})["status"] == [1975858777]


def test_an_unknown_state_fails_and_says_what_the_store_has(generated):
    """The default must never be a silently dropped filter.

    Salla does not reject a filter it cannot use -- it ignores it and returns
    every order, which reads as a complete answer. Failing here is the entire
    reason this feature exists.
    """
    with pytest.raises(generated.ToolError) as caught:
        generated.resolve_values({"status": ["refunded"]})

    message = str(caught.value)
    assert "'refunded'" in message
    assert "shipped" in message, "the caller should be told what IS available"


def test_drop_is_available_but_must_be_chosen(generated):
    source = codemode.generate(_entry(copy.deepcopy(CONTRACT)))
    assert "'on_miss': 'error'" in source, "the default must be baked in, not assumed"


def test_an_absent_argument_is_left_alone(generated):
    """No status asked for, no lookup, no filter."""
    assert generated.resolve_values({"page": 2}) == {"page": 2}


def test_the_resolved_value_is_what_reaches_the_wire(generated):
    resolved = generated.resolve_values({"status": ["shipped"], "page": 1})
    assert ("status[]", 1201821018) in generated.build_query(resolved)


# -- generation ------------------------------------------------------------


def test_a_contract_without_a_resolver_generates_no_lookup():
    plain = copy.deepcopy(CONTRACT)
    plain["binding"]["http"]["parameters"]["query"] = [{"name": "page"}]
    source = codemode.generate(_entry(plain))
    assert "'resolver': None" in source


def test_changing_a_resolver_retires_the_cached_code():
    """generation_id decides what the code cache may reuse."""
    original = _entry(copy.deepcopy(CONTRACT))

    edited_contract = copy.deepcopy(CONTRACT)
    edited_contract["binding"]["http"]["parameters"]["query"][1]["resolve"]["sendField"] = "slug"
    edited = _entry(edited_contract)

    assert codemode.generation_id(edited) != codemode.generation_id(original)


# -- the version guard -----------------------------------------------------


def test_the_current_rulebook_is_accepted():
    assert unsupported_schema("https://contract-mcp.example/schemas/tool-contract.v2.json") is None
    assert SUPPORTED_SCHEMA_IDS


def test_a_rulebook_this_engine_does_not_implement_is_refused():
    """Serving it partially is worse than not serving it.

    An engine without `resolve` would ignore the block, send "shipped" where an
    id belongs, and get every order back. Refusing is visible; that is not.
    """
    problem = unsupported_schema("https://contract-mcp.example/schemas/tool-contract.v9.json")
    assert problem and "v9" in problem


def test_a_registry_without_a_schema_id_is_still_served():
    """Predates the field, and cannot contain anything this engine mishandles."""
    assert unsupported_schema(None) is None
