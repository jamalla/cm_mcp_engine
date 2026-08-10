"""Code cache: generate a tool's source once, reuse it forever.

Key is `contractName@contractVersion+<digest of the contract>`, so a version bump
*or* an edit to the contract retires the stale code automatically -- no
invalidation logic to get wrong, and no way for a corrected contract to be served
with the code its predecessor generated.
"""

from __future__ import annotations

from pathlib import Path

from cm_engine.config import CODE_CACHE_DIR


class CodeCache:
    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or CODE_CACHE_DIR
        self._memory: dict[str, str] = {}

    def _path(self, key: str) -> Path:
        # @ and + are legal in a filename on Linux and awkward on Windows, so the
        # key is flattened rather than trusted as a path component.
        return self._dir / f"{key.replace('@', '__').replace('+', '__')}.py"

    def get(self, key: str) -> str | None:
        if key in self._memory:
            return self._memory[key]
        path = self._path(key)
        if path.exists():
            source = path.read_text(encoding="utf-8")
            self._memory[key] = source
            return source
        return None

    def put(self, key: str, source: str) -> Path:
        self._memory[key] = source
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        path.write_text(source, encoding="utf-8")
        return path

    def clear(self) -> None:
        self._memory.clear()
        if self._dir.exists():
            for path in self._dir.glob("*.py"):
                path.unlink()
