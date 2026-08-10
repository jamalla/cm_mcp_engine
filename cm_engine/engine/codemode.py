"""code_mode: contract -> runnable Python, deterministically.

No LLM. The contract already declares the API, the inputs, and the validation
rules, so generation is a template fill. That makes it fast, repeatable, and
byte-stable -- which in turn is what makes the code cache trustworthy.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from cm_engine.config import resolve_upstream, upstream_base_url
from cm_engine.registry.loader import ToolEntry

TEMPLATE_DIR = Path(__file__).parent / "codegen_templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)

# Embeds a value as a Python literal. Not `tojson`: JSON writes null/true/false,
# which read as undefined names in Python and fail at import -- the templates
# generate Python, so they need Python's spelling. repr is deterministic for the
# str/int/bool/None/list/dict shapes a contract can hold, which is what keeps
# generation byte-stable and the code cache trustworthy.
_env.filters["pyval"] = repr


def _common(entry: ToolEntry) -> dict:
    schema = entry.input_schema
    return {
        "contract_name": entry.name,
        "contract_version": entry.version,
        "required": list(schema.get("required", [])),
        "forbidden": list(entry.interface.get("input", {}).get("forbidden", [])),
        "rules": entry.rules,
    }


def _mapping(spec: dict) -> dict[str, str]:
    """A wire name and the tool argument feeding it.

    `from` defaults to `name`, which is the common case: the contract exposes
    the upstream's own parameter name because it was already a good one.
    """
    wire = spec["name"]
    return {"wire": wire, "arg": spec.get("from", wire)}


def _query_mapping(spec: dict) -> dict:
    """As above, plus array serialization and pinned constants.

    A `constant` is sent on every call and reads no argument -- the way a
    contract pins a filter the agent must not be able to change.
    """
    wire = spec["name"]
    has_constant = "constant" in spec
    return {
        "wire": wire,
        "arg": None if has_constant else spec.get("from", wire),
        "constant": spec.get("constant"),
        "has_constant": has_constant,
        "style": spec.get("style", "single"),
    }


def _http_context(entry: ToolEntry) -> dict:
    http = entry.http
    upstream = resolve_upstream(http.get("api"))
    if upstream is None:
        raise ValueError(
            f"{entry.name}: binding.http.api {http.get('api')!r} is not a configured upstream"
        )

    parameters = http.get("parameters", {}) or {}
    path_params = [_mapping(spec) for spec in parameters.get("path") or []]
    query_params = [_query_mapping(spec) for spec in parameters.get("query") or []]
    body = parameters.get("body") or {"mode": "none"}
    body_fields = [_mapping(spec) for spec in body.get("fields") or []]

    response = http.get("response", {}) or {}
    errors = response.get("errors", {}) or {}
    response_schema = entry.interface.get("response", {}).get("schema", {})

    # Which tool arguments the path, query and mapped body already account for.
    # Under body mode "passthrough" everything left over becomes the JSON body,
    # so this set is what decides where an argument ends up.
    consumed = (
        {m["arg"] for m in path_params}
        | {m["arg"] for m in query_params if m["arg"]}
        | {m["arg"] for m in body_fields}
    )

    return {
        **_common(entry),
        "upstream": upstream.name,
        # Resolved at generation time, so the code the audience reads in the
        # right pane names the host it will really call.
        "base_url": upstream_base_url(upstream),
        "token_env": upstream.token_env,
        "auth_scheme": upstream.auth_scheme,
        "scopes": entry.scopes,
        "method": http["method"],
        "path_template": http["path"],
        "timeout_ms": http.get("timeoutMs", 5000),
        "path_params": path_params,
        "query_params": query_params,
        "body_mode": body.get("mode", "none"),
        "body_fields": body_fields,
        "consumed_args": sorted(consumed),
        "data_path": response["dataPath"].split("."),
        "collection": bool(response.get("collection")),
        "pagination": response.get("pagination", "none"),
        "success_statuses": response.get("successStatuses") or [200],
        "error_message_path": (errors.get("messagePath") or "error.message").split("."),
        "error_fields_path": (errors.get("fieldsPath") or "error.fields").split("."),
        # Keyed by status so the generated code can name what a failure means
        # instead of dumping the upstream's body at the agent.
        "error_meanings": {
            str(item["status"]): {
                "meaning": item["meaning"],
                **({"retryable": item["retryable"]} if "retryable" in item else {}),
            }
            for item in errors.get("statuses") or []
        },
        "response_fields": sorted(response_schema.get("properties", {})),
    }


def _context(entry: ToolEntry) -> tuple[str, dict]:
    """The template and the values that will fill it."""
    kind = entry.binding.get("type")

    if kind == "http":
        return "http_tool.py.j2", _http_context(entry)

    if kind == "none":
        return "builtin_tool.py.j2", {**_common(entry), "handler": entry.binding["handler"]}

    raise ValueError(f"{entry.name}: unsupported binding type {kind!r}")


def generate(entry: ToolEntry) -> str:
    """Render the tool's source. Pure: same inputs in, same bytes out."""
    template, context = _context(entry)
    return _env.get_template(template).render(**context)


def generation_id(entry: ToolEntry) -> str:
    """Digest of everything that decides the generated bytes.

    The cache identity, and deliberately wider than the contract. The same
    contract generates a *different* module when the engine's upstream table
    changes or when DEV_OFFLINE swings the host between production and the local
    mock -- and the on-disk code cache outlives a restart. Keyed on the contract
    alone, turning DEV_OFFLINE off would hand back the offline module and quietly
    keep calling a mock instead of the store.

    It also covers the case that has no version bump behind it: nothing forces a
    contributor to touch contractVersion when they correct a contract, so content
    decides rather than a number someone has to remember.

    Fields that do not reach the template -- a reworded description, a new
    whenToUse hint -- do not change this, which is correct: they change what the
    agent is told, not what the code does.
    """
    template, context = _context(entry)
    canonical = json.dumps({"template": template, "context": context}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def required_secrets(entry: ToolEntry) -> list[str]:
    """Environment variables this tool's generated code is allowed to read.

    The sandbox injects these and nothing else, so a tool cannot reach a
    credential it never asked for. The name comes from the engine's upstream
    table, never from the contract -- a contract cannot request a token.
    """
    if entry.binding.get("type") != "http":
        return []
    upstream = resolve_upstream(entry.http.get("api"))
    return [upstream.token_env] if upstream else []
