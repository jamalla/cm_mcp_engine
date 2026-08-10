"""Stage events must survive the MCP notification round trip intact."""

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.dependencies import get_context

from cm_engine.events import Emitter, ListSink, StageEvent


def test_payload_round_trips():
    event = StageEvent(run_id="r1", seq=3, type="result", data={"output": {"a": 1}})
    revived = StageEvent.from_notification("result", event.payload())

    assert revived is not None
    assert (revived.run_id, revived.seq, revived.type) == ("r1", 3, "result")
    assert revived.data == {"output": {"a": 1}}


def test_ignores_unrelated_log_records():
    assert StageEvent.from_notification("info", {"msg": "hello"}) is None
    assert StageEvent.from_notification("info", {}) is None


async def test_emitter_numbers_events_monotonically():
    sink = ListSink()
    emitter = Emitter("run-x", sink)
    for stage in ("routing", "executing", "result"):
        await emitter.emit(stage)

    assert sink.types() == ["routing", "executing", "result"]
    assert [e.seq for e in sink.events] == [0, 1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        # `args` collides with a LogRecord attribute -- the proposal event carries it.
        {"args": {"orderId": "ORD-1"}, "action": "POST /cancel"},
        # `message` likewise -- every error event carries it, so this bug class
        # would have taken out error reporting first.
        {"stage": "executing", "message": "boom"},
        # A few more reserved names, so a future event cannot reintroduce this.
        {"name": "x", "module": "y", "levelname": "z", "exc_info": None},
    ],
)
async def test_reserved_logrecord_names_survive_the_notification(payload):
    """ctx.info(extra=...) builds a stdlib LogRecord, which raises on reserved
    attribute names. Nesting the payload under one key is what makes any event
    field name safe -- this test is the reason that nesting exists."""
    mcp = FastMCP("EventsTest")

    @mcp.tool
    async def emit(run_id: str) -> dict:
        ctx = get_context()
        event = StageEvent(run_id=run_id, seq=0, type="proposal", data=payload)
        await ctx.info(event.type, extra=event.payload())
        return {"emitted": True}

    received: list[StageEvent] = []

    async def log_handler(message):
        event = StageEvent.from_notification(
            message.data.get("msg", ""), message.data.get("extra") or {}
        )
        if event is not None:
            received.append(event)

    async with Client(mcp, log_handler=log_handler) as client:
        result = await client.call_tool("emit", {"run_id": "run-1"})

    assert result.data == {"emitted": True}
    assert len(received) == 1, "the notification never arrived"
    assert received[0].data == payload
