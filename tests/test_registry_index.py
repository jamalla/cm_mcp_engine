"""The registry is an index, not a directory listing.

cm_mcp_contracts publishes one file per contract plus an index carrying provenance
and a sha256 per entry. One file per contract is what keeps the engine's pin PR
reviewable at hundreds of tools; the index is what makes the directory a registry
rather than a folder someone can add to.

Which matters because the two are different claims. "Present on disk" is not
"approved": a contract dropped in afterwards was never reviewed, and one edited
after publication is no longer what the gate saw. Both are refused here, loudly,
and the rest of the registry keeps working.
"""

import hashlib
import json
from pathlib import Path

import pytest

from cm_engine.config import ContractSource
from cm_engine.registry.loader import load_catalog

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def build_index(root: Path, names: list[str]) -> Path:
    """Write a registry in the published layout: index + contracts/<name>.json.

    Bytes throughout, as the builder does. Writing in text mode here would let
    Windows rewrite the newlines and the fixtures would fail their own hashes --
    which is how the real builder's portability bug surfaced.
    """
    (root / "contracts").mkdir(parents=True, exist_ok=True)
    entries = []

    for name in names:
        body = (FIXTURES / f"{name}.json").read_bytes()
        (root / "contracts" / f"{name}.json").write_bytes(body)
        entries.append(
            {
                "name": name,
                "version": "1.0.0",
                "path": f"contracts/{name}.json",
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )

    index = root / "registry.json"
    index.write_text(
        json.dumps(
            {
                "generatedAt": "2026-08-10T00:00:00+00:00",
                "sourceRepo": "cm_mcp_contracts",
                "sourceCommit": "4fb0f76",
                "layout": "index",
                "toolCount": len(entries),
                "toolNames": sorted(names),
                "contracts": entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return index


def load(index: Path):
    return load_catalog(ContractSource("registry-file", index, "test"))


ALL = ["list_categories", "create_category", "delete_category", "estimate_delivery_window"]


def test_the_engine_serves_what_the_index_lists(tmp_path):
    catalog = load(build_index(tmp_path, ALL))

    assert {entry.name for entry in catalog.all()} == set(ALL)
    assert not catalog.warnings


def test_a_contract_edited_after_publication_is_refused(tmp_path):
    """The hash is the whole point of shipping one.

    Anyone with write access to the engine repo could otherwise change a pinned
    contract -- its path, its scopes -- without touching the index a reviewer read.
    """
    index = build_index(tmp_path, ALL)
    tampered = tmp_path / "contracts" / "list_categories.json"
    contract = json.loads(tampered.read_text(encoding="utf-8"))
    contract["binding"]["http"]["path"] = "/orders"
    tampered.write_text(json.dumps(contract, indent=2), encoding="utf-8")

    catalog = load(index)

    assert "list_categories" not in catalog
    assert any("changed after publication" in w for w in catalog.warnings)
    # The rest of the registry is unaffected: one bad entry is not an outage.
    assert "create_category" in catalog


def test_a_contract_nobody_published_is_not_served(tmp_path):
    """A file in the directory that no index entry claims."""
    index = build_index(tmp_path, ["list_categories"])
    smuggled = json.loads((FIXTURES / "delete_category.json").read_text(encoding="utf-8"))
    (tmp_path / "contracts" / "delete_category.json").write_text(
        json.dumps(smuggled), encoding="utf-8"
    )

    catalog = load(index)

    assert "delete_category" not in catalog, "unlisted means unpublished"
    assert any("not listed in the registry index" in w for w in catalog.warnings)
    assert "list_categories" in catalog


def test_a_missing_file_is_reported_against_its_name(tmp_path):
    index = build_index(tmp_path, ALL)
    (tmp_path / "contracts" / "create_category.json").unlink()

    catalog = load(index)

    assert "create_category" not in catalog
    assert any("which is missing" in w for w in catalog.warnings)


def test_a_path_cannot_escape_the_registry(tmp_path):
    """An index is data from another repository, so its paths are not trusted."""
    (tmp_path / "outside.json").write_text(
        (FIXTURES / "list_categories.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    index = build_index(tmp_path / "reg", ["create_category"])
    payload = json.loads(index.read_text(encoding="utf-8"))
    payload["contracts"].append(
        {"name": "escapee", "version": "1.0.0", "path": "../outside.json", "sha256": ""}
    )
    index.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    catalog = load(index)

    assert "escapee" not in catalog
    assert any("escapes the registry" in w for w in catalog.warnings)


def test_the_legacy_inlined_layout_still_loads(tmp_path):
    """A registry pinned before the split keeps serving until the next pin PR."""
    contracts = [
        json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")) for name in ALL
    ]
    legacy = tmp_path / "registry.generated.json"
    legacy.write_text(json.dumps({"toolCount": len(contracts), "contracts": contracts}), "utf-8")

    catalog = load(legacy)

    assert {entry.name for entry in catalog.all()} == set(ALL)
    assert not catalog.warnings


@pytest.mark.parametrize("names", [ALL, ["list_categories"]])
def test_check_registry_accepts_the_published_layout(tmp_path, names):
    """The cross-repo gate runs against exactly this shape."""
    import subprocess
    import sys

    index = build_index(tmp_path, names)
    result = subprocess.run(
        [sys.executable, "scripts/check_registry.py", str(index)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"All {len(names)} contract(s) are executable" in result.stdout
