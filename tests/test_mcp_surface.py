"""The MCP surface, over the in-memory transport -- same protocol, no ports.

These are the tests that would catch a FastMCP upgrade breaking the demo: the
tool list, the schemas, the contract:// resources, and the stage-event stream
that the UI's right pane is built on.
"""

import json

import pytest
from fastmcp import Client

from cm_engine.events import StageEvent
from cm_engine.server import executor, mcp

PARTNER_TOOLS = {
    "get_order_status",
    "estimate_delivery_window",
    "cancel_order",
    "lookup_shipping_zone",
    "check_return_window",
}


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


async def test_every_contract_is_exposed_as_an_mcp_tool():
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert PARTNER_TOOLS <= names
    assert {"list_contracts", "refresh_registry", "clear_caches"} <= names


async def test_tools_publish_the_contract_schema_and_hints():
    async with Client(mcp) as client:
        tool = next(t for t in await client.list_tools() if t.name == "get_order_status")

    assert tool.inputSchema["properties"]["orderId"]["description"] == "Merchant order ID."
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    # Routing hints travel both in the description (for a generic client) and in
    # meta (for one that understands our contract format).
    assert "Use this when" in (tool.description or "")
    assert tool.meta["whenToUse"]
    assert tool.meta["validationRules"][0]["field"] == "orderId"


async def test_run_id_is_injected_into_every_tool_schema():
    """Concurrent runs share one connection; run_id is what separates them."""
    async with Client(mcp) as client:
        for tool in await client.list_tools():
            if tool.name in PARTNER_TOOLS:
                assert "run_id" in tool.inputSchema["properties"], tool.name


async def test_contract_resource_returns_the_raw_json():
    async with Client(mcp) as client:
        contents = await client.read_resource("contract://get_order_status")
    contract = json.loads(contents[0].text)
    assert contract["interface"]["name"] == "get_order_status"
    assert contract["binding"]["type"] == "http"


async def test_stage_events_arrive_during_the_call_in_order():
    collector = Collector()
    async with Client(mcp, log_handler=collector) as client:
        result = await client.call_tool(
            "estimate_delivery_window",
            {"zone": "domestic", "speed": "standard", "run_id": "R1"},
        )

    assert result.data["status"] == "ok"
    types = collector.types_for("R1")
    assert types == [
        "contract_selected",
        "code_generated",
        "executing",
        "result",
        "cache_store",
        "done",
    ]
    seqs = [e.seq for e in collector.events if e.run_id == "R1"]
    assert seqs == sorted(seqs)


async def test_repeat_call_hits_cache_and_omits_the_work_stages():
    """AC#4 over the real protocol."""
    collector = Collector()
    args = {"zone": "international", "speed": "overnight"}

    async with Client(mcp, log_handler=collector) as client:
        await client.call_tool("estimate_delivery_window", {**args, "run_id": "COLD"})
        second = await client.call_tool("estimate_delivery_window", {**args, "run_id": "WARM"})

    assert second.data["cached"] is True
    assert "cache_store" in collector.types_for("COLD")

    warm = collector.types_for("WARM")
    assert "cache_hit" in warm
    assert "code_generated" not in warm
    assert "executing" not in warm


async def test_concurrent_runs_do_not_interleave():
    import asyncio

    collector = Collector()
    async with Client(mcp, log_handler=collector) as client:
        await asyncio.gather(
            client.call_tool("lookup_shipping_zone", {"countryCode": "SA", "run_id": "X"}),
            client.call_tool("lookup_shipping_zone", {"countryCode": "AE", "run_id": "Y"}),
        )

    for run_id in ("X", "Y"):
        seqs = [e.seq for e in collector.events if e.run_id == run_id]
        assert seqs == sorted(seqs) and len(seqs) >= 5, f"run {run_id}: {seqs}"


async def test_propose_apply_round_trip_over_mcp(mock_partner_api):
    """The apply half genuinely calls the partner API, so it needs one running."""
    collector = Collector()
    async with Client(mcp, log_handler=collector) as client:
        proposal = await client.call_tool(
            "cancel_order", {"orderId": "ORD-654321", "run_id": "P1"}
        )
        assert proposal.data["status"] == "proposed"
        token = proposal.data["approvalToken"]
        assert token

        # Nothing was executed on the first call.
        assert "executing" not in collector.types_for("P1")

        applied = await client.call_tool(
            "cancel_order",
            {"orderId": "ORD-654321", "run_id": "P2", "approval_token": token},
        )

    assert applied.data["status"] == "ok"
    assert applied.data["output"]["status"] == "cancelled"
    assert "executing" in collector.types_for("P2")


async def test_list_contracts_hides_the_binding():
    async with Client(mcp) as client:
        listing = (await client.call_tool("list_contracts", {})).data

    assert len(listing["tools"]) == len(PARTNER_TOOLS)
    assert not listing["warnings"]
    assert "binding" not in json.dumps(listing)
    assert "PARTNER_ORDERS_TOKEN" not in json.dumps(listing)
