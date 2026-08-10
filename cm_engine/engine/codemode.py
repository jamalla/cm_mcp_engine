"""code_mode: contract -> runnable Python, deterministically.

No LLM. The contract already declares the API, the inputs, and the validation
rules, so generation is a template fill. That makes it fast, repeatable, and
byte-stable -- which in turn is what makes the code cache trustworthy.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from cm_engine.config import resolve_base_url
from cm_engine.registry.loader import ToolEntry

TEMPLATE_DIR = Path(__file__).parent / "codegen_templates"

_PATH_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)


def _common(entry: ToolEntry) -> dict:
    schema = entry.input_schema
    return {
        "contract_name": entry.name,
        "contract_version": entry.version,
        "required": list(schema.get("required", [])),
        "forbidden": list(entry.interface.get("input", {}).get("forbidden", [])),
        "rules": entry.rules,
    }


def generate(entry: ToolEntry) -> str:
    """Render the tool's source. Pure: same contract in, same bytes out."""
    binding = entry.binding
    kind = binding.get("type")

    if kind == "http":
        http = binding["http"]
        auth = http.get("auth", {}) or {}
        response_schema = entry.interface.get("response", {}).get("schema", {})

        context = {
            **_common(entry),
            # The override is applied at generation time, so the generated code
            # shows the audience exactly which host it will call.
            "base_url": resolve_base_url(http["baseUrl"]),
            "method": http["method"],
            "path_template": http["path"],
            "timeout_ms": http.get("timeoutMs", 5000),
            "path_params": _PATH_PARAM.findall(http["path"]),
            "auth_type": auth.get("type", "none"),
            "secret_ref": auth.get("secretRef", ""),
            "auth_header": auth.get("header", "x-api-key"),
            "response_fields": sorted(response_schema.get("properties", {})),
        }
        return _env.get_template("http_tool.py.j2").render(**context)

    if kind == "none":
        context = {**_common(entry), "handler": binding["handler"]}
        return _env.get_template("builtin_tool.py.j2").render(**context)

    raise ValueError(f"{entry.name}: unsupported binding type {kind!r}")


def required_secrets(entry: ToolEntry) -> list[str]:
    """Secret names this tool's generated code will read from its environment.

    The sandbox injects these and nothing else.
    """
    if entry.binding.get("type") != "http":
        return []
    ref = (entry.binding.get("http", {}).get("auth", {}) or {}).get("secretRef")
    return [ref] if ref else []
