"""Spike gate for the whole architecture.

Two things the live pipeline trace depends on, neither of which is safe to assume:

1. Stage events can ride MCP log notifications -- they must reach a client's
   ``log_handler`` *during* the tool call, in order, with a structured payload intact.
2. Tools can be built at runtime from a contract's own JSON Schema, rather than
   inferred from a Python function signature.

If either of these breaks on a FastMCP upgrade, the right pane of the demo goes dark.
These tests fail loudly first.
"""

import asyncio

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools import FunctionTool
from mcp.types import ToolAnnotations


async def test_log_notifications_arrive_during_call_in_order():
    """The carrier for every stage event in the pipeline."""
    mcp = FastMCP("SpikeServer")

    @mcp.tool
    async def staged(run_id: str, ctx: Context) -> dict:
        for seq, stage in enumerate(["code_generated", "executing", "result"]):
            await ctx.info(stage, extra={"run_id": run_id, "seq": seq, "payload": {"stage": stage}})
        return {"ok": True}

    received: list = []

    async def log_handler(message):
        received.append(message)

    async with Client(mcp, log_handler=log_handler) as client:
        result = await client.call_tool("staged", {"run_id": "run-1"})

    assert result.data == {"ok": True}

    # All three arrived, and they arrived before the call returned.
    assert len(received) == 3, f"expected 3 notifications, got {[m.data for m in received]}"

    # data == {"msg": <event type>, "extra": {...}} -- the mapping the BFF decodes on.
    decoded = [(m.data["msg"], m.data["extra"]) for m in received]
    assert [msg for msg, _ in decoded] == ["code_generated", "executing", "result"]
    assert [extra["seq"] for _, extra in decoded] == [0, 1, 2]
    assert all(extra["run_id"] == "run-1" for _, extra in decoded)
    assert decoded[0][1]["payload"] == {"stage": "code_generated"}


async def test_concurrent_runs_stay_separable_by_run_id():
    """One client connection hosts many runs; run_id is what keeps traces apart."""
    mcp = FastMCP("SpikeServer")

    @mcp.tool
    async def slow(run_id: str, ctx: Context) -> dict:
        for seq in range(3):
            await ctx.info("tick", extra={"run_id": run_id, "seq": seq})
            await asyncio.sleep(0.01)
        return {"run_id": run_id}

    received: list = []

    async def log_handler(message):
        received.append(message.data["extra"])

    async with Client(mcp, log_handler=log_handler) as client:
        await asyncio.gather(
            client.call_tool("slow", {"run_id": "a"}),
            client.call_tool("slow", {"run_id": "b"}),
        )

    for run in ("a", "b"):
        seqs = [e["seq"] for e in received if e["run_id"] == run]
        assert seqs == [0, 1, 2], f"run {run} lost or reordered events: {seqs}"


async def test_tool_built_from_a_contract_supplied_json_schema():
    """Tools come from contracts, so the input schema is data -- not a function signature."""
    contract_input_schema = {
        "type": "object",
        "properties": {
            "orderId": {"type": "string", "description": "Merchant order ID."},
            "run_id": {"type": "string"},
        },
        "required": ["orderId", "run_id"],
        "additionalProperties": False,
    }

    # A contract-built tool takes **kwargs, which blocks FastMCP's signature-based
    # Context injection -- get_context() is the way in for dynamically built tools.
    async def handler(**kwargs) -> dict:
        ctx = get_context()
        await ctx.info("executing", extra={"run_id": kwargs["run_id"], "seq": 0})
        return {"orderId": kwargs["orderId"], "status": "shipped"}

    tool = FunctionTool(
        name="get_order_status",
        title="Get Order Status",
        description="Returns the current fulfillment status of a single order.",
        parameters=contract_input_schema,
        meta={"whenToUse": ["The user asks where an order is."]},
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        fn=handler,
    )

    mcp = FastMCP("SpikeServer", tools=[tool])

    events: list = []

    async def log_handler(message):
        events.append(message.data)

    async with Client(mcp, log_handler=log_handler) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["get_order_status"]

        # The contract's schema is what a generic MCP client sees.
        assert tools[0].inputSchema["properties"]["orderId"]["description"] == "Merchant order ID."
        assert tools[0].inputSchema["required"] == ["orderId", "run_id"]
        assert tools[0].annotations.readOnlyHint is True
        assert tools[0].meta["whenToUse"] == ["The user asks where an order is."]

        result = await client.call_tool(
            "get_order_status", {"orderId": "ORD-123456", "run_id": "run-9"}
        )

    assert result.data == {"orderId": "ORD-123456", "status": "shipped"}
    assert events and events[0]["msg"] == "executing"


async def test_contract_resource_round_trips_raw_json():
    """The UI reads contract JSON over MCP rather than through a bespoke endpoint."""
    mcp = FastMCP("SpikeServer")

    @mcp.resource("contract://{name}")
    async def contract(name: str) -> dict:
        return {"interface": {"name": name}, "contractVersion": "1.0.0"}

    async with Client(mcp) as client:
        result = await client.read_resource("contract://get_order_status")

    import json

    payload = json.loads(result[0].text)
    assert payload["interface"]["name"] == "get_order_status"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
