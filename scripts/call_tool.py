"""Run one contract, once, and print what happened.

The short feedback loop for the things that go wrong outside the contract: a
credential the upstream will not accept, an endpoint that moved, an envelope that
is not the shape the contract declared. Four processes and a browser is a lot of
machinery to stand up before finding out a token is the wrong kind.

Same engine as the demo -- same registry, codegen, sandbox and caches, same stage
events -- with a list for a sink instead of MCP notifications.

    python scripts/call_tool.py list_categories
    python scripts/call_tool.py list_categories '{"page": 2, "keyword": "shoes"}'
    python scripts/call_tool.py delete_category '{"categoryId": 1002}' --approve
    python scripts/call_tool.py list_categories --principal store-b --code

Exits non-zero when the call fails, so it can gate a script.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cm_engine.config import (  # noqa: E402
    DEV_OFFLINE,
    resolve_upstream,
    upstream_base_url,
)
from cm_engine.credentials import Principal, default_principal  # noqa: E402
from cm_engine.engine.executor import Executor  # noqa: E402
from cm_engine.events import ListSink  # noqa: E402


def describe_target(entry) -> str:
    """Which host this call will really reach, and how it was decided."""
    if entry.binding.get("type") != "http":
        return f"builtin {entry.binding.get('handler')} -- no network"

    upstream = resolve_upstream(entry.http.get("api"))
    if upstream is None:
        return f"api {entry.http.get('api')!r} is not configured in this engine"

    mode = "DEV_OFFLINE=1, local mock" if DEV_OFFLINE else "DEV_OFFLINE=0, real upstream"
    return f"{upstream_base_url(upstream)}{entry.http['path']}  ({mode})"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one contract from the registry.")
    parser.add_argument("tool", help="tool name, e.g. list_categories")
    parser.add_argument("args", nargs="?", default="{}", help="arguments as a JSON object")
    parser.add_argument("--principal", help="who the call is on behalf of")
    parser.add_argument("--approve", action="store_true", help="apply a propose-apply tool")
    parser.add_argument("--code", action="store_true", help="print the generated module")
    parsed = parser.parse_args()

    try:
        args = json.loads(parsed.args)
    except json.JSONDecodeError as exc:
        print(f"arguments are not valid JSON: {exc}")
        return 2

    executor = Executor()
    try:
        entry = executor.registry.catalog.get(parsed.tool)
    except KeyError as exc:
        print(exc)
        print(f"in the registry: {', '.join(e.name for e in executor.registry.catalog.all())}")
        return 2

    principal = Principal(id=parsed.principal) if parsed.principal else default_principal()

    print(f"registry  : {executor.registry.source.path}  ({executor.registry.source.origin})")
    print(f"tool      : {entry.key}")
    print(f"target    : {describe_target(entry)}")
    print(f"scopes    : {', '.join(entry.scopes) or '(none)'}")
    print(f"principal : {principal}")
    print(f"arguments : {json.dumps(args)}\n")

    sink = ListSink()
    outcome = await executor.run(parsed.tool, args, run_id="cli", sink=sink, principal=principal)

    # Two calls when asked to apply: the first returns a proposal and a token,
    # exactly as it would over MCP.
    if outcome.status == "proposed" and parsed.approve:
        print(f"proposal  : {outcome.output['action']}\napproving ...\n")
        sink = ListSink()
        outcome = await executor.run(
            parsed.tool,
            args,
            run_id="cli-apply",
            sink=sink,
            principal=principal,
            approval_token=outcome.approval_token,
        )

    print("stages    : " + " -> ".join(event.type for event in sink.events))

    if parsed.code:
        code = next((e.data["code"] for e in sink.events if e.type == "code_generated"), None)
        if code:
            print("\n--- generated module " + "-" * 55)
            print(code)
            print("-" * 76)

    print(f"\nstatus    : {outcome.status}  ({outcome.duration_ms} ms, cached={outcome.cached})")

    if outcome.status == "error":
        print(f"error     : {outcome.error}")
        return 1

    print("output    :")
    print(json.dumps(outcome.output, indent=2, ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
