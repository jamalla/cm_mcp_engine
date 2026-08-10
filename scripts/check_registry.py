"""Can this engine actually execute every contract in a registry?

This is the cross-repo gate. cm_mcp_contracts can approve a contract that is
perfectly schema-valid and still unrunnable here -- `builtin://forecast_weather`
with no such handler, an unsupported binding type, a template that will not
render. Nothing in the contracts repo can catch that, because the answer lives
in this repo's code.

consume-registry.yml runs this against a freshly published registry *before*
pinning it, so an incompatible contract fails a workflow instead of a demo.

Usage:
    python scripts/check_registry.py registry.generated.json
    python scripts/check_registry.py --contracts-dir path/to/contract/files
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cm_engine.config import ContractSource  # noqa: E402
from cm_engine.engine import codemode  # noqa: E402
from cm_engine.registry.loader import load_catalog  # noqa: E402


def summarize(lines: list[str]) -> None:
    """Write to the workflow's job summary when running in Actions."""
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a registry is executable by this engine.")
    parser.add_argument("registry", nargs="?", help="Path to a registry.generated.json")
    parser.add_argument("--contracts-dir", help="Check a contracts directory instead")
    args = parser.parse_args()

    if args.contracts_dir:
        source = ContractSource("contracts-dir", Path(args.contracts_dir), "--contracts-dir")
    elif args.registry:
        source = ContractSource("registry-file", Path(args.registry), "argument")
    else:
        parser.error("give a registry file or --contracts-dir")

    print(f"Checking {source.path} ({source.origin})\n")

    catalog = load_catalog(source)

    # A skipped contract is a hard failure here. At runtime the engine keeps
    # serving the rest; at gate time, shipping a registry the engine silently
    # drops tools from is exactly what we are trying to prevent.
    failures = list(catalog.warnings)
    checked = 0

    for entry in catalog.all():
        try:
            source_code = codemode.generate(entry)
            ast.parse(source_code)
        except Exception as exc:  # noqa: BLE001 - any codegen failure is a failure
            failures.append(f"{entry.key}: code generation failed -- {type(exc).__name__}: {exc}")
            continue

        checked += 1
        print(f"  OK  {entry.key:<40} {entry.binding.get('type'):<6} {len(source_code):>6} chars")

    print()
    lines = [
        "## Registry executability check",
        "",
        f"- source: `{source.path}`",
        f"- tools verified: **{checked}**",
        f"- problems: **{len(failures)}**",
    ]

    if failures:
        print(f"{len(failures)} contract(s) this engine cannot execute:\n")
        for failure in failures:
            print(f"  - {failure}")
        lines += ["", "### Cannot execute", ""] + [f"- {f}" for f in failures]
        lines += [
            "",
            "These contracts are valid per the schema but unrunnable here. Either add the "
            "missing capability to this engine, or revert them in `cm_mcp_contracts`.",
        ]
        summarize(lines)
        return 1

    if checked == 0:
        print("The registry contains no executable tools.")
        summarize(lines + ["", "The registry contains no executable tools."])
        return 1

    print(f"All {checked} contract(s) are executable by this engine.")
    summarize(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
