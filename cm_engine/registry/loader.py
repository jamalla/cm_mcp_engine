"""The registry: approved contracts -> a catalog of executable tools.

Contracts are authored, reviewed, and schema-validated in cm_mcp_contracts.
This engine consumes the result, from either:

  * a **contracts directory** -- a sibling checkout during development, or
  * a **registry.generated.json** -- the artifact that repo publishes on merge.

Full JSON-Schema conformance is deliberately *not* re-checked here. That is the
contracts gate's job and duplicating the rulebook would give us two versions of
it to disagree. What this module checks instead is the question the other repo
cannot answer: **can this engine actually run the contract?** A contract naming
`builtin://forecast_weather` passes every schema rule and is still unrunnable if
no such handler is registered. That gap is exactly what consume-registry.yml
catches before a bad registry is ever pinned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cm_engine.config import ContractSource, UPSTREAMS, resolve_contract_source, resolve_upstream

SUPPORTED_BINDINGS = {"http", "none"}
SUPPORTED_KINDS = {"single-tool"}

_PATH_PLACEHOLDER = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True)
class ToolEntry:
    """One executable tool -- one contract, one upstream endpoint."""

    name: str
    version: str
    interface: dict[str, Any]
    binding: dict[str, Any]
    governance: dict[str, Any]
    validation: dict[str, Any]
    raw: dict[str, Any]

    @property
    def key(self) -> str:
        """Cache key namespace: bumping contractVersion retires stale code."""
        return f"{self.name}@{self.version}"

    @property
    def annotations(self) -> dict[str, Any]:
        """MCP's ToolAnnotations, as declared. Served to clients verbatim.

        They live under `interface` because they are part of the MCP tool
        declaration, not our policy layer -- and they are read here, in one
        place, so caching and the served hints can never disagree.
        """
        return self.interface.get("annotations", {})

    @property
    def read_only(self) -> bool:
        return bool(self.annotations.get("readOnlyHint"))

    @property
    def destructive(self) -> bool:
        return bool(self.annotations.get("destructiveHint"))

    @property
    def http(self) -> dict[str, Any]:
        """The http binding body, or an empty dict for a builtin."""
        return self.binding.get("http") or {}

    @property
    def scopes(self) -> list[str]:
        return list(self.http.get("auth", {}).get("scopes", []))

    @property
    def needs_approval(self) -> bool:
        execution = self.governance.get("execution", {})
        return (
            execution.get("mode") == "propose-apply"
            or execution.get("humanApproval") == "required"
        )

    @property
    def caching(self) -> dict[str, Any]:
        return self.governance.get("caching", {})

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.interface.get("input", {}).get("schema", {})

    @property
    def rules(self) -> list[dict[str, str]]:
        return self.validation.get("rules", [])

    def public_view(self) -> dict[str, Any]:
        """What leaves the engine.

        Name, description, routing hints, input schema -- and nothing else.
        `binding` and anything secret-adjacent stay inside. This is what
        server.py turns into MCP tool definitions, so the boundary is enforced
        by the protocol rather than by convention.
        """
        return {
            "name": self.name,
            "version": self.version,
            "title": self.interface.get("title", self.name),
            "description": self.interface.get("description", ""),
            "whenToUse": self.interface.get("whenToUse", []),
            "whenNotToUse": self.interface.get("whenNotToUse", []),
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }

    def raw_contract(self) -> dict[str, Any]:
        """The contract as submitted -- what the UI displays."""
        return self.raw


def executability_problems(entry: ToolEntry) -> list[str]:
    """Why this engine could not run the contract, if it could not.

    Deliberately narrow: only the things that make execution impossible, never
    matters of taste or schema conformance.
    """
    problems: list[str] = []

    if not entry.name:
        problems.append("interface.name is missing")
    if not entry.input_schema:
        problems.append("interface.input.schema is missing, so no arguments can be validated")

    binding_type = entry.binding.get("type")
    if binding_type not in SUPPORTED_BINDINGS:
        problems.append(
            f"binding.type {binding_type!r} is not executable by this engine "
            f"(supported: {sorted(SUPPORTED_BINDINGS)})"
        )
    elif binding_type == "http":
        problems += _http_problems(entry)
    elif binding_type == "none":
        from cm_engine.engine.builtins import BUILTINS

        handler = entry.binding.get("handler")
        if handler not in BUILTINS:
            problems.append(
                f"no builtin handler registered for {handler!r} "
                f"(this engine provides: {sorted(BUILTINS)})"
            )

    if "readOnlyHint" not in entry.annotations:
        problems.append(
            "interface.annotations.readOnlyHint is missing, so caching cannot be decided safely"
        )

    return problems


def _http_problems(entry: ToolEntry) -> list[str]:
    """What would stop this engine from building the HTTP call."""
    problems: list[str] = []
    http = entry.http

    for field in ("method", "path"):
        if not http.get(field):
            problems.append(f"binding.http.{field} is missing")

    # The contract selects an upstream by name; this engine owns the host and the
    # credential behind that name. An upstream it has never been configured for
    # is the one gate the contracts repo genuinely cannot run for us.
    api = http.get("api")
    if resolve_upstream(api) is None:
        problems.append(
            f"binding.http.api {api!r} is not an upstream this engine is configured for "
            f"(configured: {sorted(UPSTREAMS)})"
        )

    response = http.get("response") or {}
    if not response.get("dataPath"):
        problems.append(
            "binding.http.response.dataPath is missing, so the engine cannot tell "
            "the payload from the envelope around it"
        )

    if not entry.scopes:
        problems.append("binding.http.auth.scopes is empty, so no credential can be authorized")

    # Every {placeholder} in the path needs a mapping saying which tool argument
    # fills it -- without one there is nothing to interpolate and the request
    # would be sent with a literal brace in the URL.
    mapped = {m.get("name") for m in (http.get("parameters", {}).get("path") or [])}
    for placeholder in _PATH_PLACEHOLDER.findall(http.get("path") or ""):
        if placeholder not in mapped:
            problems.append(
                f"path placeholder {{{placeholder}}} has no binding.http.parameters.path entry"
            )

    return problems


class Catalog:
    def __init__(self, source: ContractSource | None = None) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._warnings: list[str] = []
        self.source = source

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def warn(self, message: str) -> None:
        self._warnings.append(message)

    def add(self, entry: ToolEntry) -> None:
        if entry.name in self._tools:
            self.warn(f"duplicate tool name {entry.name!r}; keeping the first")
            return
        self._tools[entry.name] = entry

    def get(self, name: str) -> ToolEntry:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"no approved contract named {name!r}") from None

    def all(self) -> list[ToolEntry]:
        return sorted(self._tools.values(), key=lambda e: e.name)

    def public_view(self) -> list[dict[str, Any]]:
        return [entry.public_view() for entry in self.all()]


def entries_from_contract(contract: dict[str, Any]) -> list[ToolEntry]:
    """The tool a contract document defines.

    A list rather than a single entry because `kind` decides: today only
    single-tool is executable, and an unrecognized kind yields nothing rather
    than a half-built tool. Multi-tool packages are future work in both repos.
    """
    if contract.get("kind") not in SUPPORTED_KINDS:
        return []

    return [
        ToolEntry(
            name=contract.get("interface", {}).get("name", ""),
            version=contract.get("contractVersion", "0.0.0"),
            interface=contract.get("interface", {}),
            binding=contract.get("binding", {}),
            governance=contract.get("governance", {}),
            validation=contract.get("validation", {}),
            raw=contract,
        )
    ]


def _documents(source: ContractSource) -> list[tuple[str, dict[str, Any]]]:
    """Read raw contract documents from whichever source is configured."""
    if source.kind == "registry-file":
        if not source.path.is_file():
            raise FileNotFoundError(
                f"no registry at {source.path} (source: {source.origin}). "
                "Point CM_CONTRACTS_DIR at a cm_mcp_contracts checkout, or run "
                "the consume-registry workflow to pin one."
            )
        payload = json.loads(source.path.read_text(encoding="utf-8"))
        return [
            (c.get("interface", {}).get("name") or "?", c) for c in payload.get("contracts", [])
        ]

    if not source.path.is_dir():
        raise FileNotFoundError(
            f"no contracts directory at {source.path} (source: {source.origin})."
        )

    documents = []
    for path in sorted(source.path.rglob("*.json")):
        try:
            documents.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as exc:
            documents.append((path.name, {"__error__": str(exc)}))
    return documents


def load_catalog(source: ContractSource | None = None) -> Catalog:
    """Build the catalog. A contract this engine cannot run is skipped loudly.

    One unrunnable contract must not take the whole registry offline -- the rest
    of the partners' tools keep working, and the warning says exactly why.
    """
    source = source or resolve_contract_source()
    catalog = Catalog(source)

    for label, document in _documents(source):
        if "__error__" in document:
            catalog.warn(f"{label}: not valid JSON -- {document['__error__']}")
            continue

        entries = entries_from_contract(document)
        if not entries:
            kind = document.get("kind")
            catalog.warn(
                f"{label}: skipped -- kind {kind!r} is not executable by this engine "
                f"(supported: {sorted(SUPPORTED_KINDS)})"
            )
            continue

        for entry in entries:
            problems = executability_problems(entry)
            if problems:
                catalog.warn(f"{label}: skipped -- {'; '.join(problems)}")
                continue
            catalog.add(entry)

    return catalog


class Registry:
    """Holds the current catalog and can re-read it from its source."""

    def __init__(self, source: ContractSource | None = None) -> None:
        self._source = source
        self.catalog = load_catalog(self._source)

    @property
    def source(self) -> ContractSource | None:
        return self.catalog.source

    def refresh(self) -> Catalog:
        self.catalog = load_catalog(self._source)
        return self.catalog
