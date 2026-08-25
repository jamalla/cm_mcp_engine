"""The orchestrator: cache -> codegen -> sandbox -> cache, emitting as it goes.

This is where the demo's climax lives. On a result-cache hit the executor emits
`cache_hit` and returns immediately -- it does NOT emit `code_generated` or
`executing`, because those stages genuinely did not happen. The right pane is
visibly shorter and faster on the second prompt, which is the whole point.
"""

from __future__ import annotations

import hmac
import secrets as secrets_mod
import time
from dataclasses import dataclass
from typing import Any

from cm_engine import events as ev
from cm_engine.cache.code_cache import CodeCache
from cm_engine.cache.result_cache import ResultCache, cache_key, is_cacheable
from cm_engine.config import resolve_upstream, upstream_base_url
from cm_engine.credentials import Principal, default_principal, provider
from cm_engine.engine import a2ui, codemode
from cm_engine.engine.sandbox import run_module
from cm_engine.events import EmitSink, Emitter
from cm_engine.registry.loader import Registry, ToolEntry

# Signs approval tokens so an "approved" write has to come from a proposal this
# process actually issued. POC-grade: process-local, lost on restart.
_APPROVAL_SECRET = secrets_mod.token_hex(16)


def _approval_token(entry: ToolEntry, key: str) -> str:
    return hmac.new(
        _APPROVAL_SECRET.encode(), f"{entry.key}|{key}".encode(), "sha256"
    ).hexdigest()[:32]


@dataclass
class ExecutionOutcome:
    status: str  # "ok" | "proposed" | "error"
    output: Any = None
    error: str | None = None
    cached: bool = False
    duration_ms: int = 0
    approval_token: str | None = None


class Executor:
    def __init__(self, registry: Registry | None = None) -> None:
        self.registry = registry or Registry()
        self.code_cache = CodeCache()
        self.result_cache = ResultCache()

    # -- caches ----------------------------------------------------------

    def clear_caches(self) -> None:
        """Presenter reset between demo runs."""
        self.result_cache.clear()
        self.code_cache.clear()

    # -- main entry point ------------------------------------------------

    async def run(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        run_id: str,
        sink: EmitSink,
        approval_token: str | None = None,
        principal: Principal | None = None,
    ) -> ExecutionOutcome:
        # On whose behalf. A keyword argument of this method and never a member of
        # `args`, because `args` is what a language model chose -- letting it name
        # the principal would let a prompt decide whose data comes back.
        principal = principal or default_principal()
        emitter = Emitter(run_id, sink)
        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        try:
            entry = self.registry.catalog.get(tool_name)
        except KeyError as exc:
            await emitter.emit(ev.ERROR, stage=ev.CONTRACT_SELECTED, message=str(exc))
            return ExecutionOutcome("error", error=str(exc), duration_ms=elapsed())

        await emitter.emit(
            ev.CONTRACT_SELECTED,
            contractName=entry.name,
            version=entry.version,
            contract=entry.raw_contract(),
        )

        # The component tree comes from the contract, so it is known before the
        # call goes out. Sending it here is what A2UI's split is for: the shape
        # of the answer renders while the sandbox is still working, and the
        # result fills it in when it arrives.
        await self._emit_surface(emitter, entry, run_id, a2ui.structure_messages)

        generation = codemode.generation_id(entry)
        key = cache_key(entry, args, generation=generation, principal=principal.cache_scope)

        # --- the cache-hit short circuit --------------------------------
        if is_cacheable(entry):
            hit = self.result_cache.get(key)
            if hit is not None:
                await emitter.emit(
                    ev.CACHE_HIT, key=key, storedAt=hit.stored_at, expiresAt=hit.expires_at
                )
                await emitter.emit(ev.RESULT, output=hit.value, fromCache=True)
                await self._emit_data(emitter, entry, run_id, hit.value)
                duration = elapsed()
                await emitter.emit(ev.DONE, durationMs=duration, cached=True)
                return ExecutionOutcome(
                    "ok", output=hit.value, cached=True, duration_ms=duration
                )

        # --- propose-apply: stop before mutating anything ---------------
        if entry.needs_approval:
            expected = _approval_token(entry, key)
            if not approval_token:
                preview = self._proposal_preview(entry, args)
                await emitter.emit(
                    ev.PROPOSAL,
                    contractName=entry.name,
                    action=preview,
                    args=args,
                    approvalToken=expected,
                    reason="This contract requires human approval before it runs.",
                )
                duration = elapsed()
                await emitter.emit(ev.DONE, durationMs=duration, proposed=True)
                return ExecutionOutcome(
                    "proposed",
                    output={"proposed": True, "action": preview, "args": args},
                    duration_ms=duration,
                    approval_token=expected,
                )
            if not hmac.compare_digest(approval_token, expected):
                message = "approval token does not match this proposal"
                await emitter.emit(ev.ERROR, stage=ev.PROPOSAL, message=message)
                return ExecutionOutcome("error", error=message, duration_ms=elapsed())

        # --- code cache + codegen ---------------------------------------
        # The generated module carries no credential -- only the NAME of the
        # environment variable the sandbox will fill -- so one cached module
        # serves every principal. The token is injected per call, below.
        code_key = f"{entry.key}+{generation}"
        try:
            source = self.code_cache.get(code_key)
            from_cache = source is not None
            if source is None:
                source = codemode.generate(entry)
            module_path = self.code_cache.put(code_key, source)
        except Exception as exc:  # noqa: BLE001 - surfaced as an error event
            message = f"code generation failed: {exc}"
            await emitter.emit(ev.ERROR, stage=ev.CODE_GENERATED, message=message)
            return ExecutionOutcome("error", error=message, duration_ms=elapsed())

        await emitter.emit(
            ev.CODE_GENERATED,
            code=source,
            fromCache=from_cache,
            language="python",
            cacheKey=code_key,
        )

        # --- execute -----------------------------------------------------
        await emitter.emit(
            ev.EXECUTING,
            tool=entry.name,
            binding=entry.binding.get("type"),
            principal=str(principal),
        )

        injected = self._credentials(entry, principal)
        sandbox = await run_module(module_path, args, secrets=injected)

        if not sandbox.ok:
            await emitter.emit(ev.ERROR, stage=ev.EXECUTING, message=sandbox.error or "unknown")
            duration = elapsed()
            await emitter.emit(ev.DONE, durationMs=duration, failed=True)
            return ExecutionOutcome("error", error=sandbox.error, duration_ms=duration)

        await emitter.emit(ev.RESULT, output=sandbox.output, fromCache=False)
        await self._emit_data(emitter, entry, run_id, sandbox.output)

        # --- store, but only if the contract allows it -------------------
        if is_cacheable(entry):
            ttl = entry.caching.get("ttlSeconds")
            self.result_cache.put(key, sandbox.output, ttl)
            await emitter.emit(ev.CACHE_STORE, key=key, ttlSeconds=ttl)

        duration = elapsed()
        await emitter.emit(ev.DONE, durationMs=duration, cached=False)
        return ExecutionOutcome("ok", output=sandbox.output, duration_ms=duration)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _credentials(entry: ToolEntry, principal: Principal) -> dict[str, str]:
        """The env the sandbox may see: this tool's upstream token, nothing else.

        Resolved per call rather than per process, so a provider that mints or
        refreshes a per-install token drops straight in.
        """
        upstream = resolve_upstream(entry.http.get("api"))
        if upstream is None:
            return {}
        return {name: provider.resolve(upstream, principal) for name in codemode.required_secrets(entry)}

    @staticmethod
    async def _emit_surface(emitter, entry: ToolEntry, run_id: str, build) -> None:
        """Send one batch of A2UI messages, if this contract declares a surface.

        The surface id is the run id: two runs on one connection render into two
        surfaces rather than overwriting each other, and a client that missed the
        tree can tell which data belongs to what.
        """
        messages = build(entry, run_id)
        if messages:
            await emitter.emit(ev.SURFACE, surfaceId=run_id, messages=messages)

    @staticmethod
    async def _emit_data(emitter, entry: ToolEntry, run_id: str, output: Any) -> None:
        """The values the tree binds to, once there are any."""
        messages = a2ui.data_messages(entry, run_id, output)
        if messages:
            await emitter.emit(ev.SURFACE, surfaceId=run_id, messages=messages)

    @staticmethod
    def _proposal_preview(entry: ToolEntry, args: dict[str, Any]) -> str:
        """What the human is being asked to approve, as the request it becomes."""
        if entry.binding.get("type") != "http":
            return f"{entry.name}({args})"

        http = entry.http
        path = http["path"]
        # Show the wire names the upstream will see, resolved through the
        # contract's own mappings -- the approver should read the real request.
        for mapping in http.get("parameters", {}).get("path") or []:
            wire = mapping["name"]
            value = args.get(mapping.get("from", wire))
            if value is not None:
                path = path.replace("{" + wire + "}", str(value))

        upstream = resolve_upstream(http.get("api"))
        host = upstream_base_url(upstream) if upstream else "(unconfigured upstream)"
        return f"{http['method']} {host.rstrip('/')}{path}"
