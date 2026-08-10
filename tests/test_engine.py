"""Registry, code_mode, caches, and the executor's cache-hit short circuit."""

import ast
import json
import time
from pathlib import Path

import pytest

from cm_engine.cache.code_cache import CodeCache
from cm_engine.cache.result_cache import ResultCache, cache_key, is_cacheable
from cm_engine.engine import codemode
from cm_engine.engine.executor import Executor
from cm_engine.events import ListSink
from cm_engine.registry.loader import Registry, executability_problems, load_catalog

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def _contract(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# -- registry ---------------------------------------------------------------


def test_catalog_loads_every_pinned_contract(catalog):
    names = {entry.name for entry in catalog.all()}
    assert {
        "list_categories",
        "create_category",
        "delete_category",
        "estimate_delivery_window",
    } <= names
    assert not catalog.warnings


def test_public_view_never_leaks_the_binding(catalog):
    """The agent sees this. A leak here would undo the whole boundary."""
    for view in catalog.public_view():
        assert "binding" not in view
        for forbidden in ("api.salla.dev", "SALLA_ACCESS_TOKEN", "127.0.0.1"):
            assert forbidden not in json.dumps(view)
        assert {"name", "description", "whenToUse", "inputSchema"} <= set(view)


def test_annotations_are_read_from_the_interface(catalog):
    """MCP's own field names, in MCP's own half of the contract."""
    listing = catalog.get("list_categories")
    assert listing.read_only is True
    assert listing.destructive is False
    assert listing.annotations["idempotentHint"] is True

    deletion = catalog.get("delete_category")
    assert deletion.read_only is False
    assert deletion.destructive is True
    assert deletion.needs_approval is True


def test_every_fixture_contract_is_executable(catalog):
    for entry in catalog.all():
        assert executability_problems(entry) == [], entry.name


# -- the cross-repo gate: what this engine refuses -------------------------


def _entry(contract: dict):
    from cm_engine.registry.loader import entries_from_contract

    (entry,) = entries_from_contract(contract)
    return entry


def test_an_unconfigured_upstream_is_refused_not_served():
    """The gap the contracts repo cannot see.

    `api: "shopify"` satisfies every schema rule. It is still unrunnable here,
    because this engine holds no host and no credential for it -- and inventing
    one is exactly what the upstream table exists to prevent.
    """
    contract = _contract("list_categories")
    contract["binding"]["http"]["api"] = "shopify"

    problems = executability_problems(_entry(contract))
    assert any("not an upstream this engine is configured for" in p for p in problems)
    assert any("'salla'" in p for p in problems), "the message should name what IS configured"


def test_an_unregistered_builtin_is_refused_not_served():
    contract = _contract("estimate_delivery_window")
    contract["binding"]["handler"] = "builtin://forecast_weather"

    problems = executability_problems(_entry(contract))
    assert any("no builtin handler registered" in p for p in problems)


def test_an_unmapped_path_placeholder_is_refused():
    """A brace with nothing feeding it would be sent literally in the URL."""
    contract = _contract("delete_category")
    contract["binding"]["http"]["parameters"]["path"] = []

    problems = executability_problems(_entry(contract))
    assert any("{category_id} has no binding.http.parameters.path entry" in p for p in problems)


def test_an_unsupported_binding_type_is_refused():
    contract = _contract("list_categories")
    contract["binding"] = {"type": "grpc"}

    assert any("not executable by this engine" in p for p in executability_problems(_entry(contract)))


def test_a_missing_datapath_is_refused():
    contract = _contract("list_categories")
    del contract["binding"]["http"]["response"]["dataPath"]

    problems = executability_problems(_entry(contract))
    assert any("dataPath is missing" in p for p in problems)


def test_a_kind_this_engine_cannot_run_is_skipped_loudly(tmp_path):
    """Multi-tool packages are future work; a registry naming one says so."""
    artifact = tmp_path / "registry.generated.json"
    artifact.write_text(
        json.dumps({"contracts": [{"kind": "multi-tool", "interface": {"name": "bundle"}}]}),
        encoding="utf-8",
    )

    from cm_engine.config import ContractSource

    catalog = load_catalog(ContractSource("registry-file", artifact, "test"))
    assert len(catalog) == 0
    assert any("kind 'multi-tool' is not executable" in w for w in catalog.warnings)


def test_catalog_can_load_from_a_published_registry_artifact(tmp_path):
    """The deploy path: no contracts checkout, just the artifact."""
    from cm_engine.config import ContractSource

    contracts = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json"))
    ]
    artifact = tmp_path / "registry.generated.json"
    artifact.write_text(
        json.dumps({"toolCount": len(contracts), "contracts": contracts}), encoding="utf-8"
    )

    loaded = load_catalog(ContractSource("registry-file", artifact, "test"))
    assert "list_categories" in loaded
    assert not loaded.warnings


# -- code_mode --------------------------------------------------------------


def test_codegen_is_byte_stable(catalog):
    """Deterministic fill is what makes the code cache trustworthy."""
    entry = catalog.get("list_categories")
    assert codemode.generate(entry) == codemode.generate(entry)


def test_generated_code_is_syntactically_valid(catalog):
    for entry in catalog.all():
        ast.parse(codemode.generate(entry))


def test_the_upstream_host_comes_from_the_engine_not_the_contract(catalog):
    """No contract carries a base URL, yet the generated code has one."""
    source = codemode.generate(catalog.get("list_categories"))
    assert "127.0.0.1" in source, "DEV_OFFLINE should resolve to the local mock"
    assert "api.salla.dev" not in source
    assert "UPSTREAM = 'salla'" in source
    assert "baseUrl" not in json.dumps(_contract("list_categories"))


def test_validation_rules_are_compiled_into_the_source(catalog):
    source = codemode.generate(catalog.get("delete_category"))
    assert "^[0-9]+$" in source
    assert "categoryId must be a numeric category id" in source


def test_query_parameters_carry_their_wire_names_and_styles(catalog):
    source = codemode.generate(catalog.get("list_categories"))
    assert "'wire': 'page'" in source
    assert "'style': 'single'" in source


def test_path_and_body_mappings_rename_arguments(catalog):
    """camelCase tool arguments, snake_case wire names -- the contract's job."""
    deletion = codemode.generate(catalog.get("delete_category"))
    assert "{'wire': 'category_id', 'arg': 'categoryId'}" in deletion

    creation = codemode.generate(catalog.get("create_category"))
    assert "BODY_MODE = 'mapped'" in creation
    assert "{'wire': 'parent_id', 'arg': 'parentId'}" in creation


def test_declared_failures_reach_the_generated_code(catalog):
    """A 404's meaning is written once, in the contract, and ends up here."""
    source = codemode.generate(catalog.get("delete_category"))
    assert "No category exists with this id" in source
    assert "SUCCESS_STATUSES = [200, 202]" in source


def test_only_the_upstreams_own_token_is_requested(catalog):
    assert codemode.required_secrets(catalog.get("list_categories")) == ["SALLA_ACCESS_TOKEN"]
    # A builtin has no network and therefore no credential at all.
    assert codemode.required_secrets(catalog.get("estimate_delivery_window")) == []


# -- caches -----------------------------------------------------------------


def test_destructive_tools_are_never_cacheable(catalog):
    """The one rule that is a correctness bug rather than a preference."""
    assert not is_cacheable(catalog.get("delete_category"))
    assert not is_cacheable(catalog.get("create_category"))
    assert is_cacheable(catalog.get("list_categories"))


def _key(entry, args, generation="g1", principal="store-a"):
    return cache_key(entry, args, generation=generation, principal=principal)


def test_cache_key_respects_key_by(catalog):
    entry = catalog.get("list_categories")  # keyBy: page, keyword, status
    a = _key(entry, {"page": 1, "run_hint": "x"})
    b = _key(entry, {"page": 1, "run_hint": "y"})
    c = _key(entry, {"page": 2})
    assert a == b, "an argument outside keyBy must not fragment the cache"
    assert a != c


def test_two_principals_never_share_a_cached_result(catalog):
    """The one cache mistake that would be a breach rather than a bug.

    Two stores asking the identical question must not see each other's answer,
    and the principal in the key is the only thing standing between them.
    """
    entry = catalog.get("list_categories")
    args = {"page": 1}
    assert _key(entry, args, principal="store-a") != _key(entry, args, principal="store-b")


def test_a_redirected_upstream_retires_cached_code_and_results(catalog, monkeypatch):
    """Turning DEV_OFFLINE off must not hand back the module aimed at the mock.

    The contract is unchanged, so a contract-only cache identity would reuse
    code compiled against 127.0.0.1 while the engine believes it is calling the
    store. The generation digest covers the engine's own resolution too.
    """
    from cm_engine import config

    entry = catalog.get("list_categories")
    offline_id = codemode.generation_id(entry)
    offline_source = codemode.generate(entry)

    monkeypatch.setattr(config, "DEV_OFFLINE", False)
    live_id = codemode.generation_id(entry)
    live_source = codemode.generate(entry)

    assert "127.0.0.1" in offline_source and "api.salla.dev" in live_source
    assert offline_id != live_id
    assert _key(entry, {"page": 1}, generation=offline_id) != _key(
        entry, {"page": 1}, generation=live_id
    )


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


def test_an_edited_contract_does_not_reuse_the_old_generated_code(catalog):
    """Nothing forces a contributor to bump contractVersion when fixing a contract.

    Without a content digest in the cache identity, a corrected contract would be
    served with the code generated from the one it replaced -- and the only symptom
    would be a wrong answer. Found by running the pipeline against a real contract
    with one field changed and getting the previous contract's behavior.
    """
    from cm_engine.registry.loader import entries_from_contract

    original = catalog.get("list_categories")
    edited_doc = _contract("list_categories")
    edited_doc["interface"]["input"]["schema"]["required"] = ["page", "keyword"]
    (edited,) = entries_from_contract(edited_doc)

    assert edited.key == original.key, "same name, same version -- that is the trap"
    assert codemode.generation_id(edited) != codemode.generation_id(original)
    assert codemode.generate(edited) != codemode.generate(original)

    # And a cached result cannot cross the edit either.
    assert _key(edited, {"page": 1}, generation=codemode.generation_id(edited)) != _key(
        original, {"page": 1}, generation=codemode.generation_id(original)
    )


def test_prose_only_edits_do_not_pointlessly_retire_the_cache(catalog):
    """A reworded hint changes what the agent is told, not what the code does."""
    original = catalog.get("list_categories")
    reworded = _contract("list_categories")
    reworded["interface"]["whenToUse"].append("Another way of asking the same thing.")

    assert codemode.generation_id(_entry(reworded)) == codemode.generation_id(original)


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
        "list_categories", {"page": 1, "status": "sideways"}, run_id="v", sink=sink
    )
    assert outcome.status == "error"
    assert "status must be either" in (outcome.error or "")
    assert "error" in sink.types()


async def test_unknown_tool_is_an_error_event():
    executor = Executor()
    sink = ListSink()
    outcome = await executor.run("no_such_tool", {}, run_id="u", sink=sink)
    assert outcome.status == "error"
    assert sink.types() == ["error"]


async def test_propose_apply_mutates_nothing_without_a_token():
    """AC#7: a destructive write returns a proposal first."""
    executor = Executor()
    sink = ListSink()
    outcome = await executor.run(
        "delete_category", {"categoryId": 1009}, run_id="p", sink=sink
    )

    assert outcome.status == "proposed"
    assert outcome.approval_token
    proposal = next(e for e in sink.events if e.type == "proposal")
    # The approver reads the request itself, wire names and all.
    assert proposal.data["action"].startswith("DELETE ")
    assert "/categories/1009" in proposal.data["action"]
    # Nothing ran: no code was generated and no sandbox was started.
    assert "executing" not in sink.types()
    assert "code_generated" not in sink.types()


async def test_a_forged_approval_token_is_refused():
    executor = Executor()
    outcome = await executor.run(
        "delete_category",
        {"categoryId": 1008},
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
