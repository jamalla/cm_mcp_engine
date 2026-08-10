"""Registry, code_mode, caches, and the executor's cache-hit short circuit."""

import time

import pytest

from cm_engine.cache.code_cache import CodeCache
from cm_engine.cache.result_cache import ResultCache, cache_key, is_cacheable
from cm_engine.engine import codemode
from cm_engine.engine.executor import Executor
from cm_engine.events import ListSink
from cm_engine.registry.loader import Registry, executability_problems, load_catalog


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


# -- registry ---------------------------------------------------------------


def test_catalog_loads_and_expands_multi_tool_packages(catalog):
    names = {entry.name for entry in catalog.all()}
    assert {"get_order_status", "estimate_delivery_window", "cancel_order"} <= names
    # The two tools inside partner_support.multi.json must be individually callable.
    assert {"lookup_shipping_zone", "check_return_window"} <= names
    assert catalog.get("lookup_shipping_zone").package == "partner_support"
    assert not catalog.warnings


def test_public_view_never_leaks_the_binding(catalog):
    """The agent sees this. A leak here would undo the whole boundary."""
    for view in catalog.public_view():
        assert "binding" not in view
        assert "secretRef" not in str(view)
        assert {"name", "description", "whenToUse", "inputSchema"} <= set(view)


def test_every_fixture_contract_is_executable(catalog):
    for entry in catalog.all():
        assert executability_problems(entry) == [], entry.name


def test_an_unregistered_builtin_is_refused_not_served():
    """The gap the contracts repo cannot see.

    `builtin://forecast_weather` satisfies every schema rule and is still
    unrunnable here. It must be skipped with a reason, never silently served as
    a tool that fails on first call.
    """
    from cm_engine.registry.loader import Catalog, entries_from_contract

    contract = {
        "contractVersion": "1.0.0",
        "kind": "single-tool",
        "interface": {
            "name": "forecast_weather",
            "description": "Returns tomorrow's forecast for a city.",
            "whenToUse": ["The user asks about the weather."],
            "input": {"schema": {"type": "object", "properties": {"city": {"type": "string"}}}},
            "response": {"schema": {"type": "object", "properties": {}}},
        },
        "binding": {"type": "none", "handler": "builtin://forecast_weather"},
        "governance": {
            "annotations": {
                "readOnly": True,
                "destructive": False,
                "idempotent": True,
                "openWorld": False,
            },
            "execution": {"mode": "direct", "humanApproval": "never"},
            "caching": {"cacheable": False},
        },
    }

    (entry,) = entries_from_contract(contract)
    problems = executability_problems(entry)
    assert problems
    assert "no builtin handler registered" in problems[0]
    assert "forecast_weather" in problems[0]


def test_an_unsupported_binding_type_is_refused():
    from cm_engine.registry.loader import entries_from_contract

    contract = {
        "contractVersion": "1.0.0",
        "kind": "single-tool",
        "interface": {
            "name": "stream_events",
            "input": {"schema": {"type": "object"}},
            "response": {"schema": {"type": "object"}},
        },
        "binding": {"type": "grpc"},
        "governance": {"annotations": {"readOnly": True}},
    }
    (entry,) = entries_from_contract(contract)
    assert any("not executable by this engine" in p for p in executability_problems(entry))


def test_catalog_can_load_from_a_published_registry_artifact(tmp_path):
    """The deploy path: no contracts checkout, just the artifact."""
    import json

    from cm_engine.config import ContractSource

    contracts = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((__import__("pathlib").Path(__file__).parent / "fixtures" / "contracts").glob("*.json"))
    ]
    artifact = tmp_path / "registry.generated.json"
    artifact.write_text(json.dumps({"toolCount": 5, "contracts": contracts}), encoding="utf-8")

    loaded = load_catalog(ContractSource("registry-file", artifact, "test"))
    assert "get_order_status" in loaded
    assert "lookup_shipping_zone" in loaded
    assert not loaded.warnings


# -- code_mode --------------------------------------------------------------


def test_codegen_is_byte_stable(catalog):
    """Deterministic fill is what makes the code cache trustworthy."""
    entry = catalog.get("get_order_status")
    assert codemode.generate(entry) == codemode.generate(entry)


def test_validation_rules_are_compiled_into_the_source(catalog):
    entry = catalog.get("get_order_status")
    source = codemode.generate(entry)
    assert "^ORD-[0-9]{6,}$" in source
    assert "orderId must look like ORD-123456" in source


def test_generated_code_is_syntactically_valid(catalog):
    import ast

    for entry in catalog.all():
        ast.parse(codemode.generate(entry))


def test_only_declared_secrets_are_requested(catalog):
    assert codemode.required_secrets(catalog.get("get_order_status")) == ["PARTNER_ORDERS_TOKEN"]
    # A builtin has no network and therefore no secret at all.
    assert codemode.required_secrets(catalog.get("estimate_delivery_window")) == []


# -- caches -----------------------------------------------------------------


def test_destructive_tools_are_never_cacheable(catalog):
    """The one rule that is a correctness bug rather than a preference."""
    assert not is_cacheable(catalog.get("cancel_order"))
    assert is_cacheable(catalog.get("get_order_status"))


def test_cache_key_respects_key_by(catalog):
    entry = catalog.get("get_order_status")  # keyBy: ["orderId"]
    a = cache_key(entry, {"orderId": "ORD-111111", "traceId": "x"})
    b = cache_key(entry, {"orderId": "ORD-111111", "traceId": "y"})
    c = cache_key(entry, {"orderId": "ORD-222222"})
    assert a == b, "an argument outside keyBy must not fragment the cache"
    assert a != c


def test_result_cache_honours_ttl():
    cache = ResultCache()
    cache.put("k", {"v": 1}, ttl_seconds=60)
    assert cache.get("k") is not None
    assert cache.get("k", now=time.time() + 61) is None
    assert len(cache) == 0, "an expired entry should be dropped, not kept"


def test_result_cache_without_ttl_does_not_expire():
    cache = ResultCache()
    cache.put("k", {"v": 1}, ttl_seconds=None)
    assert cache.get("k", now=time.time() + 10_000) is not None


def test_code_cache_invalidates_on_version_bump(tmp_path):
    cache = CodeCache(directory=tmp_path)
    cache.put("tool@1.0.0", "old source")
    assert cache.get("tool@1.0.0") == "old source"
    assert cache.get("tool@1.1.0") is None, "a version bump must retire stale code"


# -- executor ---------------------------------------------------------------


async def test_builtin_tool_runs_with_no_network():
    """AC#5: the demo works offline."""
    executor = Executor()
    sink = ListSink()
    outcome = await executor.run(
        "estimate_delivery_window",
        {"zone": "regional", "speed": "express"},
        run_id="t1",
        sink=sink,
    )
    assert outcome.status == "ok"
    assert outcome.output["minDays"] == 2
    assert "code_generated" in sink.types()


async def test_second_identical_run_hits_cache_and_skips_work():
    """AC#4, and the whole point of the demo."""
    executor = Executor()
    executor.clear_caches()
    args = {"zone": "domestic", "speed": "standard"}

    first = ListSink()
    await executor.run("estimate_delivery_window", args, run_id="a", sink=first)

    second = ListSink()
    outcome = await executor.run("estimate_delivery_window", args, run_id="b", sink=second)

    assert "cache_store" in first.types()
    assert "code_generated" in first.types()
    assert "executing" in first.types()

    assert outcome.cached is True
    assert "cache_hit" in second.types()
    # The stages did not merely go faster -- they did not happen.
    assert "code_generated" not in second.types()
    assert "executing" not in second.types()


async def test_validation_failure_is_reported_not_raised():
    executor = Executor()
    sink = ListSink()
    outcome = await executor.run(
        "get_order_status", {"orderId": "nope"}, run_id="v", sink=sink
    )
    assert outcome.status == "error"
    assert "ORD-123456" in (outcome.error or "")
    assert "error" in sink.types()


async def test_unknown_tool_is_an_error_event():
    executor = Executor()
    sink = ListSink()
    outcome = await executor.run("no_such_tool", {}, run_id="u", sink=sink)
    assert outcome.status == "error"
    assert sink.types() == ["error"]


async def test_propose_apply_mutates_nothing_without_a_token():
    """AC#7 / brief §5.2: a write returns a proposal first."""
    executor = Executor()
    sink = ListSink()
    outcome = await executor.run(
        "cancel_order", {"orderId": "ORD-500001"}, run_id="p", sink=sink
    )

    assert outcome.status == "proposed"
    assert outcome.approval_token
    assert "proposal" in sink.types()
    # Nothing ran: no code was generated and no sandbox was started.
    assert "executing" not in sink.types()
    assert "code_generated" not in sink.types()


async def test_a_forged_approval_token_is_refused():
    executor = Executor()
    outcome = await executor.run(
        "cancel_order",
        {"orderId": "ORD-500002"},
        run_id="p2",
        sink=ListSink(),
        approval_token="not-the-real-token",
    )
    assert outcome.status == "error"
    assert "approval token" in (outcome.error or "")


async def test_registry_refresh_rereads_disk():
    registry = Registry()
    before = len(registry.catalog)
    assert len(registry.refresh()) == before
