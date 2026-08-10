"""Pins the stage-event envelope shared with cm_mcp_agent.

The two services are separate repositories with no shared package, so the only
thing holding the protocol together is that both sides agree on this literal.
The agent repo has the mirror of this file. Change the shape in one place and
one of the two goes red -- which is the point. Without it, a drift shows up as a
blank right pane in the demo and nowhere else.
"""

from cm_engine.events import ENVELOPE_KEY, StageEvent

# Byte-for-byte what an MCP notifications/message carries for one stage.
# Keep identical to cm_mcp_agent/tests/test_wire_contract.py.
NOTIFICATION = {
    "msg": "code_generated",
    "extra": {
        "stage_event": {
            "run_id": "run-abc123",
            "seq": 3,
            "ts": 1786000000.5,
            "data": {"code": "print('hi')\n", "fromCache": False, "language": "python"},
        }
    },
}


def test_envelope_key_is_the_agreed_name():
    assert ENVELOPE_KEY == "stage_event"


def test_emitted_payload_matches_the_agreed_envelope():
    """What this engine puts on the wire is what the agent expects to find."""
    event = StageEvent(
        run_id="run-abc123",
        seq=3,
        type="code_generated",
        data={"code": "print('hi')\n", "fromCache": False, "language": "python"},
        ts=1786000000.5,
    )
    assert event.payload() == NOTIFICATION["extra"]


def test_round_trips_through_the_envelope():
    event = StageEvent.from_notification(NOTIFICATION["msg"], NOTIFICATION["extra"])
    assert event is not None
    assert (event.run_id, event.seq, event.type, event.ts) == (
        "run-abc123",
        3,
        "code_generated",
        1786000000.5,
    )
    assert event.data == NOTIFICATION["extra"]["stage_event"]["data"]


def test_ordinary_server_logging_is_not_a_stage_event():
    assert StageEvent.from_notification("info", {"msg": "listening on :8765"}) is None
    assert StageEvent.from_notification("info", {}) is None
