"""Unit tests for the cross-connector ingest excludes (ADR-0020 §(b))."""

from __future__ import annotations

from pathlib import Path

import pytest

from opshub.core.errors import ConfigError
from opshub.core.excludes import ExcludeRules, excludes_path, load_excludes


def test_excludes_path_resolves_under_config_dir(tmp_path: Path) -> None:
    assert excludes_path(tmp_path) == tmp_path / "excludes.yaml"


def test_missing_file_yields_empty_rules(tmp_path: Path) -> None:
    rules = load_excludes(config_dir=tmp_path)
    assert rules.is_empty()
    assert rules.channels == frozenset()
    assert rules.paths == ()


def test_empty_file_yields_empty_rules(tmp_path: Path) -> None:
    (tmp_path / "excludes.yaml").write_text("", encoding="utf-8")
    assert load_excludes(config_dir=tmp_path).is_empty()


def test_loads_all_four_selectors(tmp_path: Path) -> None:
    (tmp_path / "excludes.yaml").write_text(
        "channels:\n"
        "  - C0SECRET01\n"
        "senders:\n"
        "  - assistant@example.com\n"
        "repos:\n"
        "  - acme/secret-vault\n"
        "paths:\n"
        "  - '**/secrets/**'\n"
        "  - '.env'\n",
        encoding="utf-8",
    )
    rules = load_excludes(config_dir=tmp_path)
    assert rules.excludes_channel("C0SECRET01")
    assert not rules.excludes_channel("C0PUBLIC02")
    assert rules.excludes_sender("assistant@example.com")
    assert rules.excludes_repo("acme/secret-vault")
    assert not rules.excludes_repo("acme/public-repo")


def test_path_glob_matches_nested_and_top_level(tmp_path: Path) -> None:
    (tmp_path / "excludes.yaml").write_text(
        "paths:\n  - '**/secrets/**'\n  - '.env'\n",
        encoding="utf-8",
    )
    rules = load_excludes(config_dir=tmp_path)
    # ``**/secrets/**`` matches both nested and top-level secrets dirs.
    assert rules.excludes_path("a/b/secrets/key.pem")
    assert rules.excludes_path("secrets/key.pem")
    # Bare basename pattern catches the file at any depth.
    assert rules.excludes_path("deep/nested/.env")
    assert rules.excludes_path(".env")
    assert not rules.excludes_path("projects/spec.md")


def test_none_inputs_never_match() -> None:
    rules = ExcludeRules(channels=frozenset({"C1"}), paths=("**/x/**",))
    assert not rules.excludes_channel(None)
    assert not rules.excludes_sender(None)
    assert not rules.excludes_repo(None)
    assert not rules.excludes_path(None)


def test_malformed_top_level_raises(tmp_path: Path) -> None:
    (tmp_path / "excludes.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping at the top level"):
        load_excludes(config_dir=tmp_path)


def test_non_string_list_raises(tmp_path: Path) -> None:
    (tmp_path / "excludes.yaml").write_text("channels:\n  - 123\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a list of strings"):
        load_excludes(config_dir=tmp_path)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / "excludes.yaml").write_text("channels: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_excludes(config_dir=tmp_path)


def test_nested_form_raises(tmp_path: Path) -> None:
    """The historical nested per-connector shape (`slack: {channels: [...]}`)
    must fail-fast — silently dropping it would let an operator believe a
    sensitive channel had been excluded when in fact `slack` was never a
    recognised selector. Pinned per Phase 11 audit Cluster C (ADR-0020 §(b))
    so the docs cannot drift back to nested examples without breaking this.
    """
    (tmp_path / "excludes.yaml").write_text(
        "slack:\n"
        "  channels:\n"
        "    - C0SECRET01\n"
        "teams:\n"
        "  channels:\n"
        "    - '19:secret-teams-channel-id'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown top-level key"):
        load_excludes(config_dir=tmp_path)


def test_single_unknown_key_also_raises(tmp_path: Path) -> None:
    """A single typo (e.g. ``channel`` instead of ``channels``) must also
    fail-fast — same rationale as the nested-form rejection above.
    """
    (tmp_path / "excludes.yaml").write_text(
        "channel:\n  - C0SECRET01\n",  # missing trailing 's'
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown top-level key"):
        load_excludes(config_dir=tmp_path)
