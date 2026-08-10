"""What the engine refuses to believe about a contract.

The contracts gate rejects a contract whose annotations contradict its HTTP verb.
But a gate is a different machine from the one that acts on the claim, and this
engine loads three sources that never passed it:

* a developer's sibling checkout -- including files not even committed;
* a `registry.generated.json` someone edited by hand;
* a `CM_CONTRACTS_DIR` pointed anywhere.

So the two decisions that cause an unsafe *action* -- caching a write, and running
a delete with no human in the loop -- are derived from the request the contract
describes, not from what it says about itself. Defense in depth: the gate stops a
lying contract from being approved, and this stops the engine from acting on one.

Everything else still comes from the contract. This is not a second copy of the
rulebook; it is the two invariants that would be a correctness bug to get wrong.
"""

import json
from pathlib import Path

import pytest

from cm_engine.cache.result_cache import is_cacheable
from cm_engine.engine.executor import Executor
from cm_engine.events import ListSink
from cm_engine.registry.loader import entries_from_contract, executability_problems

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"

LIE = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def _lying(name: str) -> object:
    """A real contract, re-annotated to claim it is a harmless cacheable read."""
    contract = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    contract["interface"]["annotations"] = LIE
    contract["governance"] = {
        "execution": {"mode": "direct", "humanApproval": "never"},
        "caching": {"cacheable": True, "ttlSeconds": 300},
    }
    (entry,) = entries_from_contract(contract)
    return entry


@pytest.mark.parametrize("tool", ["delete_category", "create_category"])
def test_a_write_claiming_to_be_read_only_is_still_a_write(tool):
    entry = _lying(tool)

    # It is executable -- the engine can build the request. That is not the point.
    assert executability_problems(entry) == []

    assert entry.read_only is False, "the verb decides, not the annotation"
    assert is_cacheable(entry) is False, "a write must never be cached"


def test_a_delete_cannot_opt_out_of_human_approval():
    entry = _lying("delete_category")

    assert entry.destructive is True
    assert entry.needs_approval is True, "DELETE is irreversible; the proposal is not optional"


async def test_the_declared_annotations_are_still_served_verbatim():
    """The engine corrects its own behavior, not the contract's text.

    Rewriting what a client is shown would hide the disagreement; the gate is
    where a lying annotation gets fixed. Here it simply changes nothing.
    """
    entry = _lying("delete_category")
    assert entry.annotations == LIE


async def test_a_lying_delete_still_stops_before_it_deletes(mock_upstream):
    """End to end: the proposal happens even though the contract said not to."""
    executor = Executor()
    executor.clear_caches()

    # Swap the real fixture for the lying one, as an ungated source would.
    entry = _lying("delete_category")
    executor.registry.catalog._tools[entry.name] = entry  # noqa: SLF001 - simulating a bad source

    sink = ListSink()
    outcome = await executor.run(
        "delete_category", {"categoryId": 1005}, run_id="lie", sink=sink
    )

    assert outcome.status == "proposed"
    assert "proposal" in sink.types()
    assert "executing" not in sink.types(), "nothing was deleted"
