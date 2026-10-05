"""Static repository checks for the deferred strategy framework."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_strategy_templates_not_in_data_directory():
    """The old active-looking template file must no longer exist in data/."""
    assert not (REPO_ROOT / "data" / "strategy_templates.json").exists()


def test_strategy_templates_proposal_exists_and_is_example():
    """The proposal file must live under docs/examples and be marked inactive."""
    proposal = REPO_ROOT / "docs" / "examples" / "strategy_templates.proposal.json"
    assert proposal.exists()
    text = proposal.read_text(encoding="utf-8")
    assert "default-fixed_interval" in text
    assert "default-budget_aware" in text


def test_strategy_framework_adr_exists():
    """The ADR documenting the deferral must exist."""
    adr = REPO_ROOT / "docs" / "adr" / "ADR-001-strategy-framework.md"
    assert adr.exists()
    text = adr.read_text(encoding="utf-8")
    assert "Defer the Pluggable Strategy Framework" in text


def test_strategy_package_has_no_runtime_code():
    """bot/strategies must contain only the placeholder README."""
    strategies_dir = REPO_ROOT / "bot" / "strategies"
    assert strategies_dir.is_dir()
    py_files = list(strategies_dir.glob("*.py"))
    assert py_files == [], f"Unexpected Python files in bot/strategies: {py_files}"
    readme = strategies_dir / "README.md"
    assert readme.exists()


def test_backup_does_not_include_strategy_templates():
    """The inactive proposal file must not be treated as production data."""
    backup_py = REPO_ROOT / "bot" / "backup.py"
    text = backup_py.read_text(encoding="utf-8")
    assert "strategy_templates.json" not in text
