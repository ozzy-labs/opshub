"""Cross-connector ingest excludes (Phase 10 step A2, ADR-0020 §(b)).

The first safety layer of Full Local Content Retention (ADR-0020) is to
**never ingest** a source the operator wants kept out of opshub. Before
ADR-0020 each connector grew its own ad-hoc filter (Phase 9 ``box_drive``
carried ``[connectors.box_drive] exclude_globs`` inline, ADR-0019 §決定
(g)). ADR-0020 §(b) consolidates those into one operator-owned file —
``$XDG_CONFIG_HOME/opshub/excludes.yaml`` — so the "what gets retained"
policy lives in one place rather than scattered across connector configs.

File shape
----------

```yaml
# ~/.config/opshub/excludes.yaml
channels:           # Slack channel ids the connector must skip
  - C0SECRET01
senders:            # author / sender identifiers (email, slack user id)
  - secretary@example.com
repos:              # GitHub "owner/repo" the connector must skip
  - acme/secret-vault
paths:              # fnmatch / gitignore-style globs (box_drive, OneDrive…)
  - "**/secrets/**"
  - "**/.env"
```

Every key is optional; a missing / empty file means "exclude nothing".
The four selectors map onto the four connector identity dimensions
ADR-0020 §(b) calls out (channel / sender / repo / path). A connector
checks only the dimensions that make sense for it (Slack → channel +
sender, GitHub → repo + sender, box_drive / OneDrive → path).

The four keys are the **only** recognised top-level keys. Historical
documentation occasionally showed a nested per-connector shape
(``slack: {channels: [...]}`` / ``teams: {channels: [...]}`` etc.); the
parser used to silently drop those because no selector named ``slack`` /
``teams`` existed, which meant an operator who copied the nested form
believed they had excluded a sensitive channel while the connector
went on ingesting it. The loader now rejects unknown top-level keys
with :class:`ConfigError` so that drift fails loud on the next sync
rather than degrading to a silent ingest of restricted content.

The ``box_drive`` connector keeps reading its own
``[connectors.box_drive] exclude_globs`` *and* honours the shared
``paths`` selector: the two are merged at the call site so an operator
can migrate inline globs to the shared file at their own pace
(ADR-0020 §(b): pre-userbase, no dual-read deprecation window — the
shared file simply augments the inline list).

PyYAML is a base dependency (the file format is YAML); a malformed file
fails fast with :class:`~opshub.core.errors.ConfigError` so a typo never
silently disables exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any, cast

from opshub.core.config import default_config_dir
from opshub.core.errors import ConfigError

__all__ = ["ExcludeRules", "excludes_path", "load_excludes"]


def excludes_path(config_dir: Path | None = None) -> Path:
    """Return the canonical excludes file path: ``<config_dir>/excludes.yaml``.

    ``config_dir`` defaults to :func:`opshub.core.config.default_config_dir`
    (``$XDG_CONFIG_HOME/opshub``) so the resolution honours the same XDG
    rules as the rest of opshub config.
    """
    base = config_dir if config_dir is not None else default_config_dir()
    return base / "excludes.yaml"


@dataclass(frozen=True, slots=True)
class ExcludeRules:
    """Resolved exclude rules across the four connector identity dimensions.

    Attributes
    ----------
    channels:
        Slack channel ids to skip (exact match).
    senders:
        Author / sender identifiers to skip (exact match — email,
        Slack user id, GitHub login).
    repos:
        GitHub ``owner/repo`` strings to skip (exact match).
    paths:
        fnmatch / gitignore-style glob patterns matched against a
        POSIX-form path (``box_drive`` / OneDrive / any FS-backed
        connector). ``"**/"`` prefix is treated as optional so a single
        pattern catches both nested and top-level files (gitignore
        intuition), mirroring
        :meth:`opshub.connectors.box_drive.scanner.BoxDriveScanner._is_excluded`.
    """

    channels: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    senders: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    repos: frozenset[str] = field(default_factory=lambda: frozenset[str]())
    paths: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """Return ``True`` when no rule of any dimension is configured."""
        return not (self.channels or self.senders or self.repos or self.paths)

    def excludes_channel(self, channel: str | None) -> bool:
        """Return ``True`` when ``channel`` is in the configured channel set."""
        return channel is not None and channel in self.channels

    def excludes_sender(self, sender: str | None) -> bool:
        """Return ``True`` when ``sender`` is in the configured sender set."""
        return sender is not None and sender in self.senders

    def excludes_repo(self, repo: str | None) -> bool:
        """Return ``True`` when ``repo`` is in the configured repo set."""
        return repo is not None and repo in self.repos

    def excludes_path(self, path: str | None) -> bool:
        """Return ``True`` when ``path`` matches any configured path glob.

        ``path`` is normalised to POSIX form before matching so the same
        rule works on WSL2 / macOS. Bare patterns without ``/`` also
        match the basename so ``".env"`` catches ``"a/b/.env"``; a
        ``"**/"`` prefix is treated as optional so ``"**/secrets/**"``
        matches both ``"secrets/k"`` and ``"a/secrets/k"``.
        """
        if path is None or not self.paths:
            return False
        posix = PurePosixPath(path).as_posix()
        basename = PurePosixPath(posix).name
        for pattern in self.paths:
            if fnmatch(posix, pattern):
                return True
            if "/" not in pattern and fnmatch(basename, pattern):
                return True
            if pattern.startswith("**/") and fnmatch(posix, pattern.removeprefix("**/")):
                return True
        return False

    def merged_with_paths(self, extra_paths: list[str]) -> ExcludeRules:
        """Return a copy with ``extra_paths`` appended to the path globs.

        Used by the ``box_drive`` connector to fold its inline
        ``[connectors.box_drive] exclude_globs`` into the shared rules
        without losing either source (ADR-0020 §(b)).
        """
        if not extra_paths:
            return self
        return ExcludeRules(
            channels=self.channels,
            senders=self.senders,
            repos=self.repos,
            paths=tuple(self.paths) + tuple(extra_paths),
        )


def load_excludes(config_dir: Path | None = None) -> ExcludeRules:
    """Load and parse ``excludes.yaml``, returning resolved :class:`ExcludeRules`.

    A missing file yields empty rules (exclude nothing). A present file
    that is not a YAML mapping, or whose selector values are not lists
    of strings, raises :class:`ConfigError` so a malformed config fails
    fast rather than silently disabling exclusion (a silent failure
    would retain content the operator explicitly wanted kept out — the
    worst outcome for ADR-0020 §(b)).
    """
    path = excludes_path(config_dir)
    if not path.exists():
        return ExcludeRules()

    # PyYAML is a base dependency (excludes is core to ADR-0020), so the
    # import never fails on a default install; keep it local so the CLI
    # cold-start path (ADR-0001) does not pay for it unless excludes are
    # actually loaded.
    import yaml

    try:
        loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"excludes.yaml is not valid YAML ({path}): {exc}") from exc

    if loaded is None:
        return ExcludeRules()
    if not isinstance(loaded, dict):
        raise ConfigError(f"excludes.yaml must be a mapping at the top level ({path})")

    raw = cast(dict[str, object], loaded)

    # Reject unknown top-level keys to fail-fast on the historical
    # **nested** shape (``slack: { channels: [...] }`` /
    # ``teams: { channels: [...] }`` etc.) that earlier docs accidentally
    # documented. Silently ignoring those nested mappings used to let an
    # operator believe they had excluded a sensitive channel when in
    # fact ``slack`` is not a recognised selector at all — the worst
    # possible outcome for ADR-0020 §(b) ("never ingest"). Raise so the
    # operator notices on the *next* connector sync and corrects the
    # file, rather than discovering the silent skip via a data audit.
    allowed = {"channels", "senders", "repos", "paths"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"excludes.yaml has unknown top-level key(s) {unknown!r} ({path});"
            f" allowed top-level keys are {sorted(allowed)!r} (ADR-0020 §(b) flat"
            " schema — nested per-connector forms like 'slack: {channels: [...]}'"
            " are not accepted)"
        )

    return ExcludeRules(
        channels=frozenset(_string_list(raw, "channels", path)),
        senders=frozenset(_string_list(raw, "senders", path)),
        repos=frozenset(_string_list(raw, "repos", path)),
        paths=tuple(_string_list(raw, "paths", path)),
    )


def _string_list(raw: dict[str, object], key: str, path: Path) -> list[str]:
    """Coerce ``raw[key]`` into a ``list[str]``, raising on a malformed value."""
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"excludes.yaml key {key!r} must be a list of strings ({path})")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise ConfigError(f"excludes.yaml key {key!r} must be a list of strings ({path})")
    return cast(list[str], items)
