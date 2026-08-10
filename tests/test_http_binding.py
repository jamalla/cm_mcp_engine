"""The generated HTTP code, run for real against a Salla-shaped upstream.

Codegen producing parseable Python proves nothing about whether the request it
builds is the one the contract described. These tests execute it: arguments
become a real query string, the envelope is unwrapped, the response is projected
down to what the contract promised, and each documented failure comes back as
the sentence the contract wrote for it.

Every test here needs the offline upstream, which the `mock_upstream` fixture
starts in-process -- so they pass in a clean checkout with nothing else running.
"""

import json

import pytest

from cm_engine.engine.executor import Executor
from cm_engine.events import ListSink

pytestmark = pytest.mark.usefixtures("mock_upstream")


@pytest.fixture
def executor():
    engine = Executor()
    engine.clear_caches()
    return engine


async def run(executor, tool, args, **kwargs):
    return await executor.run(tool, args, run_id="http", sink=ListSink(), **kwargs)


# -- reads ------------------------------------------------------------------


async def test_a_list_endpoint_unwraps_the_envelope_and_keeps_the_pagination(executor):
    outcome = await run(executor, "list_categories", {"page": 1})

    assert outcome.status == "ok", outcome.error
    # The {status, success, data} wrapper is gone; the payload is what is left.
    assert set(outcome.output) == {"items", "count", "pagination"}
    assert outcome.output["count"] == 5
    assert outcome.output["pagination"]["totalPages"] == 2
    assert outcome.output["pagination"]["currentPage"] == 1


async def test_pagination_is_projected_too(executor):
    """The upstream sends more beside the data than the contract promises.

    Salla's pagination object carries a prebuilt `links.next` URL with the
    connected app's id in it. The agent paginates with the tool's own `page`
    argument, so that URL has no business travelling into a model's context.
    """
    outcome = await run(executor, "list_categories", {"page": 1})

    assert set(outcome.output["pagination"]) == {
        "count",
        "total",
        "perPage",
        "currentPage",
        "totalPages",
    }
    assert "connected_app_id" not in json.dumps(outcome.output)


async def test_the_response_is_projected_to_the_fields_the_contract_promised(executor):
    outcome = await run(executor, "list_categories", {"page": 1})

    first = outcome.output["items"][0]
    assert set(first) == {"id", "name", "parent_id", "status", "sort_order", "image"}
    # The upstream sends these too. A contract that did not ask for them does
    # not get them, so adding a field upstream cannot leak it to an agent.
    assert "urls" not in first
    assert "updated_at" not in first


async def test_query_arguments_actually_filter_the_call(executor):
    """Proof the mapping reached the wire, not just the generated constants."""
    keyword = await run(executor, "list_categories", {"page": 1, "keyword": "sneak"})
    assert [c["name"] for c in keyword.output["items"]] == ["Sneakers"]

    hidden = await run(executor, "list_categories", {"page": 1, "status": "hidden"})
    assert hidden.output["items"], "the status filter should match something"
    assert {c["status"] for c in hidden.output["items"]} == {"hidden"}


async def test_pagination_walks(executor):
    page_two = await run(executor, "list_categories", {"page": 2})
    assert page_two.output["count"] == 4
    assert page_two.output["pagination"]["currentPage"] == 2


# -- writes -----------------------------------------------------------------


async def test_a_mapped_body_renames_arguments_on_the_way_out(executor):
    """parentId is the tool's name for it; parent_id is the upstream's."""
    outcome = await run(
        executor, "create_category", {"name": "Winter Sale", "parentId": 1001}
    )

    assert outcome.status == "ok", outcome.error
    assert outcome.output["name"] == "Winter Sale"
    assert outcome.output["parent_id"] == 1001
    # 201 is declared as success for this endpoint, and treated as one.
    assert outcome.output["id"]


async def test_a_path_parameter_reaches_the_url(executor):
    """The delete path is /categories/{category_id}, fed by categoryId."""
    proposal = await run(executor, "delete_category", {"categoryId": 1002})
    applied = await run(
        executor,
        "delete_category",
        {"categoryId": 1002},
        approval_token=proposal.approval_token,
    )

    assert applied.status == "ok", applied.error
    assert applied.output == {"id": 1002, "deleted": True}


# -- failures ---------------------------------------------------------------


async def test_a_documented_404_comes_back_as_the_contract_wrote_it(executor):
    proposal = await run(executor, "delete_category", {"categoryId": 1999})
    outcome = await run(
        executor,
        "delete_category",
        {"categoryId": 1999},
        approval_token=proposal.approval_token,
    )

    assert outcome.status == "error"
    # The contract's own words, not a dump of the upstream body.
    assert "No category exists with this id" in outcome.error
    # And the upstream's message alongside it, for the reviewer reading logs.
    assert "No category exists with the id 1999" in outcome.error
    assert "retrying" not in outcome.error, "a 404 is not worth retrying"


async def test_a_422_names_the_field_that_was_rejected(executor):
    outcome = await run(executor, "create_category", {"name": "no"})

    assert outcome.status == "error"
    assert "rejected as invalid" in outcome.error
    assert "name: The name must be at least 3 characters." in outcome.error


async def test_an_undocumented_status_says_so_and_flags_the_retry(executor):
    """The mock's one fault injection: keyword=boom returns 503.

    Nothing in list_categories documents a 503, and the engine says exactly
    that instead of pretending the contract covered it.
    """
    outcome = await run(executor, "list_categories", {"page": 1, "keyword": "boom"})

    assert outcome.status == "error"
    assert "does not document" in outcome.error
    assert "retrying the call unchanged may succeed" in outcome.error


async def test_a_read_result_is_cached_and_the_repeat_is_fast(executor):
    """The demo's climax, over a real HTTP call with ~400ms of latency."""
    first = await run(executor, "list_categories", {"page": 1})
    second = await run(executor, "list_categories", {"page": 1})

    assert first.cached is False
    assert second.cached is True
    assert second.output == first.output
    assert second.duration_ms < first.duration_ms


async def test_a_write_is_never_served_from_cache(executor):
    """Two identical creates must both reach the upstream."""
    first = await run(executor, "create_category", {"name": "Twice"})
    second = await run(executor, "create_category", {"name": "Twice"})

    assert first.cached is False and second.cached is False
