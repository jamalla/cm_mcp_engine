"""Result cache: the demo's climax, and a correctness boundary.

Written ONLY when the contract says the tool is read-only and cacheable. A
destructive tool is never stored, and that rule lives in exactly one place
(`is_cacheable`) so it cannot be half-enforced.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    stored_at: float
    expires_at: float | None

    def expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or time.time()) >= self.expires_at


def is_cacheable(entry) -> bool:
    """The single gate on writing a result.

    `entry` is a registry ToolEntry. Both conditions must hold: the annotations
    must say readOnly, and the caching policy must opt in. A destructive tool
    fails the first test even if someone sets cacheable by mistake.
    """
    return bool(entry.read_only) and bool(entry.caching.get("cacheable"))


def cache_key(entry, args: dict[str, Any], *, generation: str, principal: str) -> str:
    """Everything that must match before a stored result may be reused.

    Four things, and each one is a bug if left out:

    * **the tool** -- `entry.key`, so two tools cannot collide;
    * **the generation** -- a digest of what the code was generated from, so a
      corrected contract or a redirected upstream cannot be answered from the
      previous version's results;
    * **the principal** -- so two merchants asking the same question never see
      each other's data. This is the one that would be a breach rather than a
      bug, so it is a required argument with no default;
    * **the arguments** `caching.keyBy` says matter -- keyBy lets a contract
      declare that only some arguments affect the result, so a trace id does not
      fragment the cache.
    """
    key_by = entry.caching.get("keyBy")
    relevant = {k: args[k] for k in key_by if k in args} if key_by else dict(args)
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    material = f"{entry.key}|{generation}|{principal}|{canonical}"
    return f"{entry.key}:{hashlib.sha256(material.encode()).hexdigest()[:16]}"


class ResultCache:
    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str, now: float | None = None) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expired(now):
            del self._entries[key]
            return None
        return entry

    def put(self, key: str, value: Any, ttl_seconds: int | None) -> CacheEntry:
        now = time.time()
        entry = CacheEntry(
            value=value,
            stored_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
        )
        self._entries[key] = entry
        return entry

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
