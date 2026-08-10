"""Whose data comes back is decided by the engine, never by the prompt.

The confused-deputy problem, in its MCP shape: the arguments an agent sends are
chosen by a language model, and anything reachable from a prompt is reachable by
an attacker who can put text in front of that model. So the principal -- the
merchant whose credential is used and whose cached results are read -- is a
parameter of the call the *engine* makes, not a field in the arguments.

These tests are cheap and the failure they guard against is not.
"""

import pytest

from cm_engine.credentials import EnvCredentials, Principal, default_principal
from cm_engine.engine.executor import Executor
from cm_engine.events import ListSink
from cm_engine.server import _input_schema, registry


class RecordingCredentials(EnvCredentials):
    """Remembers who a token was minted for."""

    def __init__(self) -> None:
        self.asked_for: list[str] = []

    def resolve(self, upstream, principal):
        self.asked_for.append(principal.id)
        return f"token-for-{principal.id}"


@pytest.fixture
def recorder(monkeypatch):
    provider = RecordingCredentials()
    monkeypatch.setattr("cm_engine.engine.executor.provider", provider)
    return provider


def test_no_tool_exposes_a_principal_argument():
    """A contract cannot offer the agent a store to choose."""
    reserved = {"principal", "store", "store_id", "storeId", "merchant", "merchant_id", "tenant"}
    for entry in registry.catalog.all():
        exposed = set(_input_schema(entry).get("properties", {}))
        assert not (exposed & reserved), f"{entry.name} exposes {exposed & reserved}"


async def test_an_argument_named_principal_does_not_change_whose_token_is_used(
    recorder, mock_upstream
):
    """The trap: a prompt talks the model into sending principal="victim-store"."""
    executor = Executor()
    executor.clear_caches()

    await executor.run(
        "list_categories",
        {"page": 1, "principal": "victim-store"},
        run_id="attack",
        sink=ListSink(),
    )

    assert recorder.asked_for == [default_principal().id]
    assert "victim-store" not in recorder.asked_for


async def test_the_caller_the_engine_names_is_the_one_credited(recorder, mock_upstream):
    executor = Executor()
    executor.clear_caches()

    await executor.run(
        "list_categories",
        {"page": 1},
        run_id="explicit",
        sink=ListSink(),
        principal=Principal(id="store-42", label="Store 42"),
    )

    assert recorder.asked_for == ["store-42"]


async def test_one_principals_cached_result_is_not_served_to_another(recorder, mock_upstream):
    """Same tool, same arguments, two merchants -- two upstream calls."""
    executor = Executor()
    executor.clear_caches()
    args = {"page": 1}

    first = await executor.run(
        "list_categories", args, run_id="a", sink=ListSink(), principal=Principal(id="store-a")
    )
    second = await executor.run(
        "list_categories", args, run_id="b", sink=ListSink(), principal=Principal(id="store-b")
    )
    repeat = await executor.run(
        "list_categories", args, run_id="a2", sink=ListSink(), principal=Principal(id="store-a")
    )

    assert first.cached is False
    assert second.cached is False, "store-b must not read store-a's cached answer"
    assert repeat.cached is True, "store-a may still read its own"
    assert recorder.asked_for == ["store-a", "store-b"]


async def test_the_trace_says_who_the_call_was_made_for(recorder, mock_upstream):
    """An audience, and an audit log, should be able to see it."""
    executor = Executor()
    executor.clear_caches()
    sink = ListSink()

    await executor.run(
        "list_categories",
        {"page": 1},
        run_id="traced",
        sink=sink,
        principal=Principal(id="store-7", label="Store 7"),
    )

    executing = next(event for event in sink.events if event.type == "executing")
    assert executing.data["principal"] == "Store 7"


async def test_a_credential_is_never_written_into_the_generated_code(recorder, mock_upstream):
    """The module names the variable; the sandbox supplies the value."""
    executor = Executor()
    executor.clear_caches()
    sink = ListSink()

    await executor.run(
        "list_categories",
        {"page": 1},
        run_id="leak",
        sink=sink,
        principal=Principal(id="store-9"),
    )

    code = next(event for event in sink.events if event.type == "code_generated").data["code"]
    assert "token-for-store-9" not in code
    assert "SALLA_ACCESS_TOKEN" in code, "the NAME is what gets baked in"
