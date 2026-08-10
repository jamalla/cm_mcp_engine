"""The engine reads its own repository. Nothing else, and never by default.

What it serves is the registry the pipeline published, verified, and a human merged
into this repo. It used to auto-detect a sibling `../cm_mcp_contracts/contracts`
checkout, which was convenient and wrong: that directory holds whatever someone is
editing, on whatever branch, including contracts that never passed the gate -- and
an engine reading it serves unapproved tools while reporting a tool count that
looks perfectly legitimate.

The rule is worth a test rather than a comment, because the convenience that broke
it would be re-added in one line by anyone who found local development awkward.
"""

import os
from pathlib import Path

import pytest

from cm_engine import config

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_overrides(monkeypatch):
    """A deployed engine's environment: nothing pointing anywhere."""
    monkeypatch.delenv("CM_REGISTRY_FILE", raising=False)
    monkeypatch.delenv("CM_CONTRACTS_DIR", raising=False)


def test_the_default_source_is_inside_this_repo(no_overrides):
    source = config.resolve_contract_source()

    assert REPO_ROOT in source.path.parents or source.path.parent == REPO_ROOT, source.path
    assert source.kind == "registry-file", "the default is a published registry, not a directory"


def test_no_default_ever_resolves_to_the_contracts_repo(no_overrides):
    source = config.resolve_contract_source()

    assert "cm_mcp_contracts" not in str(source.path)


def test_the_config_holds_no_path_to_another_repo():
    """Not even an unused constant: a path that exists gets used eventually."""
    text = (REPO_ROOT / "cm_engine" / "config.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "cm_mcp_contracts" not in code, "config.py names another repository in live code"


def test_reaching_outside_takes_a_deliberate_environment_variable(monkeypatch, tmp_path):
    """The escape hatch exists, requires an explicit act, and says it is unapproved."""
    monkeypatch.delenv("CM_REGISTRY_FILE", raising=False)
    monkeypatch.setenv("CM_CONTRACTS_DIR", str(tmp_path))

    source = config.resolve_contract_source()

    assert source.path == tmp_path
    # Whoever reads a log, a list_contracts response, or the UI sees the caveat.
    assert "unapproved" in source.origin


def test_a_missing_pinned_registry_is_an_error_not_a_fallback(no_overrides, monkeypatch):
    """Serving nothing beats silently serving something unapproved.

    The failure a fresh clone should get is "no registry pinned yet", not a quiet
    substitution of whatever happens to be nearby.
    """
    from cm_engine.registry.loader import load_catalog

    monkeypatch.setattr(config, "PINNED_REGISTRY", Path(os.devnull) / "registry.json")
    monkeypatch.setattr(config, "LEGACY_REGISTRY", Path(os.devnull) / "legacy.json")

    with pytest.raises(FileNotFoundError) as exc:
        load_catalog(config.resolve_contract_source())

    assert "consume-registry" in str(exc.value), "the message should name the way to fix it"
