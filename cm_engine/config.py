"""Paths, ports, upstream APIs, and where approved contracts come from."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

CACHE_DIR = REPO_ROOT / ".cache"
CODE_CACHE_DIR = CACHE_DIR / "code"

# The registry artifact published by cm_mcp_contracts and pinned into this repo by
# the consume-registry workflow. This is what a deployed engine serves.
#
# An index plus one file per contract, so the pin PR that updates it stays
# reviewable when there are hundreds of tools: a diff shows which contract changed
# rather than a churn of one enormous file.
PINNED_REGISTRY = REPO_ROOT / "registry" / "registry.json"

# The single inlined file this repo pinned before the split. Read when no index is
# present, so an engine that has not taken a new pin PR yet keeps serving.
LEGACY_REGISTRY = REPO_ROOT / "registry.generated.json"

# There is deliberately no path to a cm_mcp_contracts checkout here. This engine
# reads its own repository: the registry the pipeline published and a human merged.
# It used to auto-detect a sibling checkout, which was convenient and wrong -- the
# contracts repo's working tree contains whatever someone is editing, on whatever
# branch, including files that never passed the gate, and an engine that reads it
# is serving unapproved tools while reporting a tool count that looks legitimate.

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))
MOCK_API_PORT = int(os.environ.get("MOCK_API_PORT", "8787"))

DEV_OFFLINE = os.environ.get("DEV_OFFLINE", "1") == "1"


# --------------------------------------------------------------------------
# Upstream APIs
# --------------------------------------------------------------------------
# A contract names an upstream (`binding.http.api`) and stops there. It carries
# no host and no credential -- which is the whole point: a contributor cannot
# point a merchant's OAuth token at a server of their choosing, and the day
# Salla moves a host or rotates a token, nothing in the registry has to change.
#
# Adding an upstream is a change to THIS table, reviewed in THIS repo, by the
# people who hold the credentials. That is the trust boundary.


@dataclass(frozen=True)
class Upstream:
    """One API this engine knows how to call."""

    name: str
    base_url: str
    token_env: str
    auth_scheme: str = "bearer"
    # Where DEV_OFFLINE sends the calls instead. The mock speaks the same
    # envelope, so the generated code is identical either way.
    mock_base_url: str | None = None


UPSTREAMS: dict[str, Upstream] = {
    "salla": Upstream(
        name="salla",
        base_url="https://api.salla.dev/admin/v2",
        token_env="SALLA_ACCESS_TOKEN",
        auth_scheme="bearer",
        mock_base_url=f"http://127.0.0.1:{MOCK_API_PORT}/admin/v2",
    ),
}

# The schema defaults `binding.http.api` to this, so a contract that omits the
# field means Salla. Kept in sync with tool-contract.v1.json by convention --
# the loader reports an unknown upstream loudly either way.
DEFAULT_UPSTREAM = "salla"


class ContractSource:
    """Where this engine instance is reading approved contracts from.

    `kind` is "registry-file" or "contracts-dir"; `path` is the resolved
    location; `origin` explains which rule won, so a surprising catalog is one
    log line away from being understood instead of a debugging session.
    """

    def __init__(self, kind: str, path: Path, origin: str) -> None:
        self.kind = kind
        self.path = path
        self.origin = origin

    def __repr__(self) -> str:
        return f"<ContractSource {self.kind} {self.path} ({self.origin})>"


def resolve_contract_source() -> ContractSource:
    """Where this engine reads contracts from.

    **The rule: the engine only ever reads its own repository.** What it serves is
    the registry the pipeline published, verified, and a human merged here -- not
    another repository's working tree, and never by default.

    Two explicit overrides remain, both requiring someone to set an environment
    variable, both reported by `list_contracts` and shown in the UI as unapproved:

      1. CM_REGISTRY_FILE  -- a registry index, which is how CI verifies a
         candidate before it is pinned, and how a developer runs against a registry
         they built locally.
      2. CM_CONTRACTS_DIR  -- a plain directory of contract files, with no index and
         so no provenance and no hashes. This repo's own tests use it against
         pinned fixtures. Pointing it at a contracts checkout works and is the
         thing the rule exists to stop being the default: nothing in that directory
         has necessarily passed the gate.

    With neither set there is exactly one answer, and it lives in this repo.
    """
    if explicit := os.environ.get("CM_REGISTRY_FILE"):
        return ContractSource("registry-file", Path(explicit), "CM_REGISTRY_FILE")

    if explicit := os.environ.get("CM_CONTRACTS_DIR"):
        return ContractSource("contracts-dir", Path(explicit), "CM_CONTRACTS_DIR (unapproved)")

    if LEGACY_REGISTRY.is_file() and not PINNED_REGISTRY.is_file():
        return ContractSource("registry-file", LEGACY_REGISTRY, "pinned registry (legacy layout)")

    return ContractSource("registry-file", PINNED_REGISTRY, "pinned registry/registry.json")


def resolve_upstream(api: str | None) -> Upstream | None:
    """Look up a contract's target upstream, or None if this engine has no such API.

    Returning None rather than raising lets the registry skip one unrunnable
    contract with a reason instead of taking the whole catalog offline.
    """
    return UPSTREAMS.get(api or DEFAULT_UPSTREAM)


def upstream_base_url(upstream: Upstream) -> str:
    """The host the generated code will actually call.

    Resolved at generation time, so the code in the demo's right pane names the
    host it really talks to -- no hidden indirection between what the audience
    reads and what runs.
    """
    if DEV_OFFLINE and upstream.mock_base_url:
        return upstream.mock_base_url
    return upstream.base_url


def resolve_token(upstream: Upstream) -> str:
    """The credential for an upstream, read from this engine's environment.

    Contracts declare the scopes a credential must carry; they never name, hold,
    or route one. A missing token is not fatal for the POC -- the offline mock
    accepts anything -- but it says so, so a real misconfiguration cannot pass
    for a working demo.
    """
    value = os.environ.get(upstream.token_env)
    if value:
        return value
    print(
        f"[config] {upstream.token_env} is not set; "
        f"calls to {upstream.name} will carry a placeholder token."
    )
    return f"missing-token:{upstream.token_env}"
