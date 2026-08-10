"""Paths, ports, secret resolution, and where approved contracts come from."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

CACHE_DIR = REPO_ROOT / ".cache"
CODE_CACHE_DIR = CACHE_DIR / "code"

# The registry artifact published by cm_mcp_contracts and pinned into this repo
# by the consume-registry workflow. This is what a deployed engine serves.
VENDORED_REGISTRY = REPO_ROOT / "registry.generated.json"

# Sibling checkout, for working in the container layout during development.
SIBLING_CONTRACTS = REPO_ROOT.parent / "cm_mcp_contracts" / "contracts"

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8765"))
MOCK_API_PORT = int(os.environ.get("MOCK_API_PORT", "8787"))

MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

DEV_OFFLINE = os.environ.get("DEV_OFFLINE", "1") == "1"

BASE_URL_OVERRIDES = {
    "https://api.partner.example": f"http://127.0.0.1:{MOCK_API_PORT}",
}


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
    """Pick a contracts source, most explicit first.

    The engine and the contracts live in separate repositories, so there is no
    single right answer -- a developer wants their sibling checkout, CI wants a
    downloaded artifact, and a deployed engine wants the version pinned into
    this repo. Rather than guess, each of those gets its own rung.
    """
    if explicit := os.environ.get("CM_REGISTRY_FILE"):
        return ContractSource("registry-file", Path(explicit), "CM_REGISTRY_FILE")

    if explicit := os.environ.get("CM_CONTRACTS_DIR"):
        return ContractSource("contracts-dir", Path(explicit), "CM_CONTRACTS_DIR")

    if SIBLING_CONTRACTS.is_dir():
        return ContractSource(
            "contracts-dir", SIBLING_CONTRACTS, "sibling cm_mcp_contracts checkout"
        )

    return ContractSource("registry-file", VENDORED_REGISTRY, "vendored registry.generated.json")


def resolve_base_url(base_url: str) -> str:
    if DEV_OFFLINE:
        return BASE_URL_OVERRIDES.get(base_url.rstrip("/"), base_url)
    return base_url


def resolve_secret(secret_ref: str) -> str:
    """Look up a declared secret by name.

    Contracts reference secrets, never carry them. A missing secret is not fatal
    for the POC -- the mock partner API accepts anything -- but it is loud, so a
    real misconfiguration does not look like a working demo.
    """
    value = os.environ.get(secret_ref)
    if value:
        return value
    print(f"[config] secretRef {secret_ref} is not set; sending a placeholder token.")
    return f"missing-secret:{secret_ref}"
