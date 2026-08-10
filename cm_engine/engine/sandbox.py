"""Run generated code in a subprocess with a timeout and a scrubbed environment.

NOT a security boundary. A subprocess shares the filesystem and the network with
its parent; this stops accidents and runaway loops, not a determined attacker.
Running untrusted partner code would need a container or a jailed interpreter,
which the POC explicitly leaves out of scope.

What it does buy: a real process boundary, a hard timeout, and an environment
carrying only the secrets the contract declared -- so a tool cannot read a
credential it never asked for.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from cm_engine.config import REPO_ROOT

# Passed through so the child can start Python and import the engine package.
# Everything else in the parent environment is dropped.
_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL")


@dataclass
class SandboxResult:
    ok: bool
    output: object = None
    error: str | None = None
    duration_ms: int = 0


def _child_env(secrets: dict[str, str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    # The generated builtin template imports mcp_engine; the http template does not.
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(secrets)
    return env


async def run_module(
    module_path: Path,
    args: dict,
    *,
    secrets: dict[str, str] | None = None,
    timeout_seconds: float = 15.0,
) -> SandboxResult:
    """Execute a generated module, feeding args on stdin and reading JSON back."""
    loop = asyncio.get_running_loop()
    started = loop.time()

    def elapsed() -> int:
        return int((loop.time() - started) * 1000)

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(module_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_child_env(secrets or {}),
            cwd=str(REPO_ROOT),
        )
    except OSError as exc:
        return SandboxResult(False, error=f"could not start sandbox: {exc}", duration_ms=elapsed())

    payload = json.dumps(args).encode("utf-8")

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload), timeout=timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return SandboxResult(
            False,
            error=f"tool exceeded its {timeout_seconds}s sandbox timeout",
            duration_ms=elapsed(),
        )

    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip() or "no stderr"
        return SandboxResult(
            False,
            error=f"sandbox exited {process.returncode}: {detail[-500:]}",
            duration_ms=elapsed(),
        )

    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        return SandboxResult(False, error="tool produced no output", duration_ms=elapsed())

    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return SandboxResult(
            False,
            error=f"tool wrote non-JSON to stdout: {text[:300]}",
            duration_ms=elapsed(),
        )

    if not envelope.get("ok"):
        return SandboxResult(
            False, error=envelope.get("error", "unknown tool error"), duration_ms=elapsed()
        )

    return SandboxResult(True, output=envelope.get("result"), duration_ms=elapsed())
