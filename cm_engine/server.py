"""The FastMCP server -- the runtime entrypoint, not a side surface.

Every approved contract becomes a real MCP tool here, built from the contract's
own JSON Schema. The UI does not import the engine; it speaks MCP to this
process, and the pipeline trace it renders is carried by MCP log notifications
emitted from inside each tool call.

Run:  uv run python -m cm_engine.server
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools import FunctionTool
from mcp.types import ToolAnnotations

from cm_engine.config import MCP_HOST, MCP_PORT
from cm_engine.credentials import Principal, default_principal
from cm_engine.engine.executor import Executor
from cm_engine.events import StageEvent
from cm_engine.registry.loader import Registry, ToolEntry

registry = Registry()
executor = Executor(registry)


async def _ctx_sink(event: StageEvent) -> None:
    """Forward a stage event to the MCP client as a structured log notification.

    FastMCP's structured logger delivers this to the client as
    `LogMessage.data == {"msg": <event type>, "extra": <payload>}`, which the
    BFF decodes straight back into a StageEvent.
    """
    ctx = get_context()
    await ctx.info(event.type, extra=event.payload())


def _describe(entry: ToolEntry) -> str:
    """Fold the routing hints into the description.

    They also ride in `meta` for structured access, but putting them here means
    a generic MCP client with no knowledge of our contract format still gets the
    guidance an agent needs to route correctly.
    """
    parts = [entry.interface.get("description", "")]

    if hints := entry.interface.get("whenToUse"):
        parts.append("\n\nUse this when:\n" + "\n".join(f"- {h}" for h in hints))
    if hints := entry.interface.get("whenNotToUse"):
        parts.append("\n\nDo not use this when:\n" + "\n".join(f"- {h}" for h in hints))
    if entry.needs_approval:
        parts.append(
            "\n\nThis tool requires human approval: the first call returns a proposal "
            "and changes nothing. Call again with the returned approvalToken to apply."
        )
    return "".join(parts)


def _input_schema(entry: ToolEntry) -> dict[str, Any]:
    """The contract's input schema, plus the fields the engine needs.

    run_id correlates this call's events on a connection shared by concurrent
    runs; approval_token carries a human's decision back for propose-apply.
    """
    schema = json.loads(json.dumps(entry.input_schema))  # deep copy; never mutate the registry
    properties = schema.setdefault("properties", {})
    properties["run_id"] = {
        "type": "string",
        "description": "Correlation id for this run's stage events. Any unique string.",
    }
    if entry.needs_approval:
        properties["approval_token"] = {
            "type": "string",
            "description": "Token from a prior proposal. Omit to receive a proposal first.",
        }
    # The contract may close the object; our injected fields must still be allowed.
    schema["additionalProperties"] = False
    return schema


def _principal() -> Principal:
    """Who this call is made on behalf of.

    Derived here, from the server's own view of the caller -- deliberately not
    from the tool's arguments, which a language model wrote. A deployment maps
    the authenticated MCP session to the merchant install behind it and returns
    that; this POC serves a single store and says so.
    """
    return default_principal()


def _build_tool(entry: ToolEntry) -> FunctionTool:
    async def handler(**kwargs: Any) -> dict[str, Any]:
        run_id = kwargs.pop("run_id", None) or f"run-{entry.name}"
        approval_token = kwargs.pop("approval_token", None)

        outcome = await executor.run(
            entry.name,
            kwargs,
            run_id=run_id,
            sink=_ctx_sink,
            approval_token=approval_token,
            principal=_principal(),
        )
        return {
            "status": outcome.status,
            "output": outcome.output,
            "error": outcome.error,
            "cached": outcome.cached,
            "durationMs": outcome.duration_ms,
            "approvalToken": outcome.approval_token,
        }

    # The contract declares these with MCP's own field names, so they are served
    # through unchanged -- no translation layer to disagree with the review that
    # approved them.
    annotations = entry.annotations
    return FunctionTool(
        name=entry.name,
        title=entry.interface.get("title", entry.name),
        description=_describe(entry),
        parameters=_input_schema(entry),
        annotations=ToolAnnotations(
            title=entry.interface.get("title"),
            readOnlyHint=annotations.get("readOnlyHint", False),
            destructiveHint=annotations.get("destructiveHint", True),
            idempotentHint=annotations.get("idempotentHint", False),
            openWorldHint=annotations.get("openWorldHint", True),
        ),
        meta={
            "contractVersion": entry.version,
            "whenToUse": entry.interface.get("whenToUse", []),
            "whenNotToUse": entry.interface.get("whenNotToUse", []),
            # Input constraints are part of the public surface -- a client needs
            # them to build a valid call. The binding they guard stays private.
            "validationRules": entry.rules,
            "governance": entry.governance,
            "responseUi": entry.interface.get("response", {}).get("ui"),
            # The scopes the merchant's credential must carry. Useful to a client
            # explaining a 401; it names no secret and no host.
            "requiredScopes": entry.scopes,
        },
        fn=handler,
    )


def build_server() -> FastMCP:
    mcp = FastMCP(
        "contract-mcp",
        instructions=(
            "Tools here are defined by partner-submitted contracts, not by server code. "
            "Each tool's description carries explicit when-to-use and when-not-to-use "
            "guidance -- read it before choosing."
        ),
        tools=[_build_tool(entry) for entry in registry.catalog.all()],
    )

    for warning in registry.catalog.warnings:
        print(f"[registry] {warning}")

    @mcp.tool
    async def list_contracts() -> dict[str, Any]:
        """List every approved contract with its routing hints and input schema."""
        return {
            "tools": registry.catalog.public_view(),
            "warnings": registry.catalog.warnings,
            # Which registry this engine is serving. When the UI shows a tool
            # the contracts repo does not have (or vice versa), this is the
            # first thing worth looking at.
            "source": {
                "kind": registry.source.kind,
                "path": str(registry.source.path),
                "origin": registry.source.origin,
            },
        }

    @mcp.tool
    async def refresh_registry() -> dict[str, Any]:
        """Re-read contracts from disk.

        Newly added tools need a server restart to appear as MCP tools; this
        refreshes the catalog the executor and `list_contracts` serve.
        """
        catalog = registry.refresh()
        return {"toolCount": len(catalog), "warnings": catalog.warnings}

    @mcp.tool
    async def clear_caches() -> dict[str, Any]:
        """Drop both cache layers. The presenter's reset between demo runs."""
        executor.clear_caches()
        return {"cleared": True}

    @mcp.resource("contract://{name}")
    async def contract_resource(name: str) -> dict[str, Any]:
        """The raw contract JSON -- this is how the UI gets the JSON it shows."""
        entry = registry.catalog.get(name)
        return entry.raw_contract()

    return mcp


mcp = build_server()


def main() -> None:
    source = registry.source
    # Which contracts this instance picked up is the first thing to check when
    # the catalog looks wrong, so say it before serving anything.
    print(f"contracts: {source.path}  ({source.origin})")
    print(f"contract-mcp serving {len(registry.catalog)} tools on http://{MCP_HOST}:{MCP_PORT}/mcp")
    mcp.run(transport="http", host=MCP_HOST, port=MCP_PORT, show_banner=False)


if __name__ == "__main__":
    main()
