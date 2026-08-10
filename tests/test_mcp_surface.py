"""The MCP surface, over the in-memory transport -- same protocol, no ports.

These are the tests that would catch a FastMCP upgrade breaking the demo: the
tool list, the schemas, the annotations, the contract:// resources, and the
stage-event stream the UI's right pane is built on.

Deliberately registry-agnostic. consume-registry.yml runs this file against a
*candidate* registry downloaded from cm_mcp_contracts, whose tool list is
whatever was just merged there -- so nothing here may name a particular tool.
Every assertion holds for any registry, which is what makes this file usable as
the cross-repo gate. Behavior specific to the pinned fixtures lives in
test_engine.py and test_http_binding.py.
"""

import json

import pytest
from fastmcp import Client

from cm_engine.events import StageEvent
from cm_engine.registry.loader import ToolEntry
from cm_engine.server import executor, mcp, registry

META_TOOLS = {"list_contracts", "refresh_registry", "clear_caches"}

CONTRACT_TOOLS = {entry.name for entry in registry.catalog.all()}


def _first(predicate) -> ToolEntry | None:
    return next((e for e in registry.catalog.all() if predicate(e)), None)


# Which tool each execution test drives. A builtin is preferred where one
# exists -- it needs no upstream at all -- but any tool that runs without
# approval will do, because these tests assert the pipeline's shape rather than
# any particular answer. The offline upstream is started for them, so a
# Salla-shaped candidate contract really executes; one whose endpoint the mock
# does not serve still produces a full trace, ending in a reported error.
OFFLINE_TOOL = _first(lambda e: e.binding.get("type") == "none")
DIRECT_TOOL = OFFLINE_TOOL or _first(lambda e: not e.needs_approval)
CACHEABLE_TOOL = _first(lambda e: e.caching.get("cacheable") and not e.needs_approval)
APPROVAL_TOOL = _first(lambda e: e.needs_approval)


def _args_for(entry: ToolEntry, **overrides) -> dict:
    """Plausible arguments from the contract's own input schema.

    Enums and defaults give a usable value for every tool the fixtures and the
    published registry contain; anything else is skipped rather than guessed at.
    """
    schema = entry.input_schema
    args = {}
    for name in schema.get("required", []):
        spec = schema.get("properties", {}).get(name, {})
        if "default" in spec:
            args[name] = spec["default"]
        elif spec.get("enum"):
            args[name] = spec["enum"][0]
        elif spec.get("type") == "integer":
            args[name] = 1
        elif spec.get("type") == "string":
            args[name] = "x"
        else:
            pytest.skip(f"cannot synthesize a value for {entry.name}.{name}")
    return {**args, **overrides}


class Collector:
    """A client-side log_handler, exactly as the BFF implements it."""

    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    async def __call__(self, message) -> None:
        data = message.data
        if not isinstance(data, dict):
            return
        event = StageEvent.from_notification(data.get("msg", ""), data.get("extra") or {})
        if event is not None:
            self.events.append(event)

    def types_for(self, run_id: str) -> list[str]:
        return [e.type for e in self.events if e.run_id == run_id]


@pytest.fixture(autouse=True)
def _clean_caches():
    executor.clear_caches()
    yield
    executor.clear_caches()


# -- the registry is served ------------------------------------------------


def test_the_registry_is_not_empty():
    """A registry serving nothing would pass every other test in this file."""
    assert CONTRACT_TOOLS, "no contracts loaded -- check the registry source"
    assert not registry.catalog.warnings, registry.catalog.warnings


async def test_every_contract_is_exposed_as_an_mcp_tool():
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names >= CONTRACT_TOOLS
    assert names >= META_TOOLS


async def test_every_tool_publishes_its_schema_hints_and_annotations():
    """The contract's four halves, as any MCP client would see them."""
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    for entry in registry.catalog.all():
        tool = tools[entry.name]
        declared = entry.annotations

        # Annotations are served under MCP's own names, straight from the contract.
        assert tool.annotations.readOnlyHint is declared["readOnlyHint"], entry.name
        assert tool.annotations.destructiveHint is declared["destructiveHint"], entry.name
        assert tool.annotations.idempotentHint is declared["idempotentHint"], entry.name
        assert tool.annotations.openWorldHint is declared["openWorldHint"], entry.name

        # Routing hints travel both in the description (for a generic client) and
        # in meta (for one that understands our contract format).
        assert "Use this when" in (tool.description or ""), entry.name
        assert tool.meta["whenToUse"] == entry.interface["whenToUse"], entry.name
        assert tool.meta["contractVersion"] == entry.version, entry.name

        # The declared arguments are all there, plus the correlation id.
        for argument in entry.input_schema.get("properties", {}):
            assert argument in tool.inputSchema["properties"], f"{entry.name}.{argument}"
        assert "run_id" in tool.inputSchema["properties"], entry.name


async def test_a_tool_needing_approval_says_so_in_its_description():
    if APPROVAL_TOOL is None:
        pytest.skip("this registry has no propose-apply tool")

    async with Client(mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == APPROVAL_TOOL.name)

    assert "requires human approval" in (tool.description or "")
    assert "approval_token" in tool.inputSchema["properties"]


async def test_contract_resources_return_the_raw_json():
    async with Client(mcp) as client:
        for entry in registry.catalog.all():
            contents = await client.read_resource(f"contract://{entry.name}")
            contract = json.loads(contents[0].text)
            assert contract["interface"]["name"] == entry.name
            assert contract["kind"] == "single-tool"


async def test_list_contracts_hides_the_binding():
    async with Client(mcp) as client:
        listing = (await client.call_tool("list_contracts", {})).data

    assert len(listing["tools"]) == len(CONTRACT_TOOLS)
    assert not listing["warnings"]
    # Whatever the registry holds, the binding never crosses the boundary.
    published = json.dumps(listing)
    assert "binding" not in published
    assert "SALLA_ACCESS_TOKEN" not in published
    assert "api.salla.dev" not in published


# -- the stage-event stream -------------------------------------------------


@pytest.mark.usefixtures("mock_upstream")
async def test_the_whole_pipeline_runs_and_reports_every_stage():
    """codegen -> sandbox -> execution, traced over MCP notifications.

    Asserts the trace rather than the answer, so it means the same thing for a
    contract this file has never seen. A tool whose endpoint the offline mock
    does not serve still emits the full sequence and ends in a reported error --
    which is itself worth proving.
    """
    if DIRECT_TOOL is None:
        pytest.skip("this registry has no tool that runs without approval")

    collector = Collector()
    async with Client(mcp, log_handler=collector) as client:
        result = await client.call_tool(
            DIRECT_TOOL.name, {**_args_for(DIRECT_TOOL), "run_id": "R1"}
        )

    types = collector.types_for("R1")
    assert types[:3] == ["contract_selected", "code_generated", "executing"]
    assert types[-1] == "done"
    # Either it produced a result or it said why not -- never silence.
    assert ("result" in types) or ("error" in types)
    if result.data["status"] == "ok":
        assert "result" in types

    seqs = [e.seq for e in collector.events if e.run_id == "R1"]
    assert seqs == sorted(seqs)


@pytest.mark.usefixtures("mock_upstream")
async def test_repeat_call_hits_cache_and_omits_the_work_stages():
    """AC#4 over the real protocol."""
    if CACHEABLE_TOOL is None:
        pytest.skip("this registry has no cacheable tool")

    collector = Collector()
    args = _args_for(CACHEABLE_TOOL)

    async with Client(mcp, log_handler=collector) as client:
        first = await client.call_tool(CACHEABLE_TOOL.name, {**args, "run_id": "COLD"})
        if first.data["status"] != "ok":
            pytest.skip(f"{CACHEABLE_TOOL.name} could not complete: {first.data['error']}")
        second = await client.call_tool(CACHEABLE_TOOL.name, {**args, "run_id": "WARM"})

    assert second.data["cached"] is True
    assert "cache_store" in collector.types_for("COLD")

    warm = collector.types_for("WARM")
    assert "cache_hit" in warm
    # The stages did not merely go faster -- they did not happen.
    assert "code_generated" not in warm
    assert "executing" not in warm


@pytest.mark.usefixtures("mock_upstream")
async def test_concurrent_runs_do_not_interleave():
    """One connection hosts many runs; run_id is what separates them."""
    if DIRECT_TOOL is None:
        pytest.skip("this registry has no tool that runs without approval")

    import asyncio

    collector = Collector()
    base = _args_for(DIRECT_TOOL)
    async with Client(mcp, log_handler=collector) as client:
        await asyncio.gather(
            client.call_tool(DIRECT_TOOL.name, {**base, "run_id": "X"}),
            client.call_tool(DIRECT_TOOL.name, {**base, "run_id": "Y"}),
        )

    for run_id in ("X", "Y"):
        seqs = [e.seq for e in collector.events if e.run_id == run_id]
        assert seqs == sorted(seqs) and len(seqs) >= 4, f"run {run_id}: {seqs}"


async def test_a_write_returns_a_proposal_and_mutates_nothing():
    """The first call to an approval-gated tool must not execute."""
    if APPROVAL_TOOL is None:
        pytest.skip("this registry has no propose-apply tool")

    collector = Collector()
    async with Client(mcp, log_handler=collector) as client:
        proposal = await client.call_tool(
            APPROVAL_TOOL.name, {**_args_for(APPROVAL_TOOL), "run_id": "P1"}
        )

    assert proposal.data["status"] == "proposed"
    assert proposal.data["approvalToken"]
    assert "proposal" in collector.types_for("P1")
    assert "executing" not in collector.types_for("P1")
    assert "code_generated" not in collector.types_for("P1")
