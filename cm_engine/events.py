"""Typed stage events -- the pipeline trace the UI renders.

The executor emits these as it works rather than returning one final blob. It
never knows how they travel: under FastMCP the sink forwards to
`ctx.info(type, extra=payload)` and they ride MCP log notifications to the BFF;
in tests the sink is a list.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# The brief's minimum set, plus two the demo needs.
PROMPT_RECEIVED = "prompt_received"
ROUTING = "routing"
CONTRACT_SELECTED = "contract_selected"
CODE_GENERATED = "code_generated"
EXECUTING = "executing"
RESULT = "result"
CACHE_STORE = "cache_store"
CACHE_HIT = "cache_hit"
ERROR = "error"
PROPOSAL = "proposal"  # propose-apply: a write awaiting human approval
SURFACE = "surface"  # A2UI messages: the component tree, then the data it binds
DONE = "done"  # carries total durationMs

# The single key every stage event is nested under inside an MCP notification's
# `extra`. Both sides of the boundary agree on this name and nothing else.
ENVELOPE_KEY = "stage_event"

# The whole vocabulary. Emitter checks against it, so a typo in an event name
# fails here instead of showing up as a stage the UI silently never renders --
# which is exactly the sort of bug that survives to a demo.
EVENT_TYPES = frozenset(
    {
        PROMPT_RECEIVED,
        ROUTING,
        CONTRACT_SELECTED,
        CODE_GENERATED,
        EXECUTING,
        RESULT,
        CACHE_STORE,
        CACHE_HIT,
        ERROR,
        PROPOSAL,
        SURFACE,
        DONE,
    }
)


@dataclass
class StageEvent:
    run_id: str
    seq: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def payload(self) -> dict[str, Any]:
        """The dict that rides an MCP notification's `extra` field.

        Nested under a single key on purpose. `ctx.info(extra=...)` builds a
        stdlib LogRecord, which raises on any key that collides with a reserved
        attribute -- and two of our payloads do: `proposal` carries `args`, and
        every `error` carries `message`. Spreading the payload flat means error
        reporting is the first thing to break. One wrapper key makes the whole
        class of collision impossible, whatever a future event carries.
        """
        return {
            ENVELOPE_KEY: {
                "run_id": self.run_id,
                "seq": self.seq,
                "ts": self.ts,
                "data": self.data,
            }
        }

    @classmethod
    def from_notification(cls, msg: str, extra: dict[str, Any]) -> StageEvent | None:
        """Rebuild an event on the client side of an MCP log notification.

        Returns None for ordinary server logging that is not one of ours.
        """
        envelope = (extra or {}).get(ENVELOPE_KEY)
        if not isinstance(envelope, dict) or "run_id" not in envelope:
            return None
        return cls(
            run_id=envelope["run_id"],
            seq=envelope.get("seq", 0),
            type=msg,
            data=envelope.get("data") or {},
            ts=envelope.get("ts", time.time()),
        )


class EmitSink(Protocol):
    """Where events go. The executor is written against this, nothing more."""

    async def __call__(self, event: StageEvent) -> None: ...


class Emitter:
    """Stamps a monotonic seq per run so the UI can order and dedupe."""

    def __init__(self, run_id: str, sink: EmitSink) -> None:
        self.run_id = run_id
        self._sink = sink
        self._seq = 0

    async def emit(self, event_type: str, **data: Any) -> StageEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"unknown stage event {event_type!r}; add it to EVENT_TYPES")
        event = StageEvent(run_id=self.run_id, seq=self._seq, type=event_type, data=data)
        self._seq += 1
        await self._sink(event)
        return event


class ListSink:
    """Collects events in memory. Used by tests and by the offline agent path."""

    def __init__(self) -> None:
        self.events: list[StageEvent] = []

    async def __call__(self, event: StageEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]
