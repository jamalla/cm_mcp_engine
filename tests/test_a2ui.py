"""The contract's surface, on the wire as A2UI.

The engine's job here is translation, not generation. A2UI's own framing has the
agent emitting the interface; ours has it declared in a reviewed contract and
merely carried, which is the difference between a UI someone approved and a UI a
language model invented while answering.

The split is also what makes the trace useful. `updateComponents` is knowable the
moment a contract is selected, so it goes out before the upstream is called; only
`updateDataModel` has to wait for the answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cm_engine import events as ev
from cm_engine.engine import a2ui
from cm_engine.events import ListSink
from cm_engine.registry.loader import entries_from_contract

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def kind(message: dict) -> str:
    """The message type. `version` rides on every envelope, so skip it."""
    return next(key for key in message if key != "version")


def entry_for(name: str):
    doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    (entry,) = entries_from_contract(doc)
    return entry


@pytest.fixture
def surfaced():
    return entry_for("list_categories")


def test_structure_goes_out_before_any_data(surfaced):
    messages = a2ui.structure_messages(surfaced, "run-1")

    assert [kind(m) for m in messages] == ["createSurface", "updateComponents"]
    assert all(m["version"] == "v0.9.1" for m in messages)

    create = messages[0]["createSurface"]
    assert create == {"surfaceId": "run-1", "catalogId": "a2ui.org:basic"}

    components = messages[1]["updateComponents"]["components"]
    assert messages[1]["updateComponents"]["surfaceId"] == "run-1"
    assert any(c["id"] == "root" for c in components), "A2UI requires a component called root"


def test_contract_annotations_are_stripped(surfaced):
    """`_comment` explains a contract to a reviewer; A2UI would reject it.

    The catalog sets `unevaluatedProperties: false`, so a stray annotation makes
    the message invalid against the schema the client validates with -- and the
    surface silently fails to render rather than reporting anything.
    """
    components = a2ui.structure_messages(surfaced, "run-1")[1]["updateComponents"]["components"]

    assert any("_comment" in c for c in surfaced.interface["response"]["ui"]["components"])
    for component in components:
        assert not [key for key in component if key.startswith("_")], component


def test_the_result_becomes_the_data_model(surfaced):
    output = {"items": [{"id": 1, "name": "Shoes", "status": "active"}], "count": 1}
    (message,) = a2ui.data_messages(surfaced, "run-1", output)

    update = message["updateDataModel"]
    assert message["version"] == "v0.9.1"
    assert update["surfaceId"] == "run-1"
    # Placed at the root unchanged: that is what makes the contract's own
    # pointers -- /items, /count, and `name` inside the row template -- resolve.
    assert update["value"] == output


def test_a_contract_without_a_surface_emits_nothing():
    """Raw JSON is a legitimate rendering, and older contracts have no surface.

    A v1 or v2 contract carries the old display hint, which has no `components`.
    Nothing is built for it rather than something wrong being built.
    """
    plain = entry_for("estimate_delivery_window")

    assert a2ui.surface_of(plain) is None
    assert a2ui.structure_messages(plain, "run-1") == []
    assert a2ui.data_messages(plain, "run-1", {"days": 3}) == []


@pytest.mark.usefixtures("mock_upstream")
async def test_the_executor_emits_both_halves_in_order():
    """Tree first, data after the call -- the ordering the trace depends on."""
    from cm_engine.engine.executor import Executor

    sink = ListSink()
    executor = Executor()
    outcome = await executor.run("list_categories", {"page": 1}, run_id="run-9", sink=sink)
    assert outcome.status == "ok", outcome.error

    surfaces = [e for e in sink.events if e.type == ev.SURFACE]
    assert len(surfaces) == 2, sink.types()

    kinds = [kind(m) for event in surfaces for m in event.data["messages"]]
    assert kinds == ["createSurface", "updateComponents", "updateDataModel"]

    stages = sink.types()
    assert stages.index(ev.CONTRACT_SELECTED) < stages.index(ev.SURFACE)
    assert stages.index(ev.EXECUTING) < stages.index(ev.RESULT)
    # The tree is sent before the upstream is called; only the data waits.
    assert stages.index(ev.SURFACE) < stages.index(ev.EXECUTING)

    assert all(e.data["surfaceId"] == "run-9" for e in surfaces)
