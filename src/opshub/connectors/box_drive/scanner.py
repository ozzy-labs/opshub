"""Box Drive scanner — pure FS walker, stat-only (Phase 9, ADR-0019).

The scanner walks a local Box Drive mount point recursively and yields
:class:`ScannedFile` records for files whose ``fingerprint`` differs
from a caller-supplied prior map (or that are not in the prior map at
all). It is intentionally **pure**: no event emission, no projection
writes, no SQL access. The Phase 9 step B2 :class:`Connector`
implementation will compose it with the mapper and ``SourceService``.

ADR-0019 §不変条件 (b) — *the scanner must never read file contents*.
Reading would force CldAPI (Microsoft) / File Provider Extension
(macOS / iCloud) placeholders to hydrate, which (i) triggers SaaS
network egress in violation of operator IT policy, (ii) inflates the
Box Drive local cache, (iii) fires OS notifications, and (iv) breaks
the External Content Minimization invariant from ADR-0005. To make the
invariant load-bearing rather than aspirational, the scanner relies
solely on ``os.scandir()`` + ``DirEntry.stat()`` (no
``open()`` / ``read_text`` / ``read_bytes`` / magic-byte sniffing).
A companion test in ``tests/unit/connectors/box_drive/test_scanner.py``
patches ``builtins.open`` and ``Path.open`` / ``read_text`` /
``read_bytes`` to raise on call, exercising a full ``scan()`` to prove
the scanner never reaches a file-content code path even by accident.

Identity = ``rel_path`` (root-relative POSIX-style path string), per
ADR-0019 §決定 (c). Rename / move surfaces as "old path stops, new
path starts" because the scanner has no way to correlate inodes across
Box Drive placeholders (xattr / ADS are not supported by the Box Drive
client). The MVP accepts that trade-off; Phase 9.x may revisit it.

Diff detection = ``fingerprint = f"{size}:{mtime_ns}"`` per
ADR-0019 §決定 (d). The caller passes the in-memory dict it built
from ``SELECT external_id, fingerprint FROM sources WHERE
connector_name = 'box_drive'``; the scanner short-circuits files whose
fingerprint matches.

File *removal* is intentionally **not** detected here (ADR-0019 §決定
(e) — Phase 9 MVP is scan-only / additive). A future
``opshub source list --stale`` CLI will derive missing files from the
``sources`` projection if the operator needs it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opshub.core.errors import ConfigError
from opshub.core.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """A single Box Drive file observation surfaced by :class:`BoxDriveScanner`.

    Attributes
    ----------
    rel_path:
        POSIX-style path relative to the scanner's ``root_path``. Used
        directly as ``SourceObserved.external_id`` by the mapper layer
        (ADR-0019 §決定 (c)); grep-friendly on purpose (no hashing).
    size:
        ``DirEntry.stat().st_size``. Stored separately even though it
        appears in :attr:`fingerprint` so the mapper / Phase 9.x consumers
        can use it without re-parsing the colon-separated string.
    mtime_ns:
        ``DirEntry.stat().st_mtime_ns``. Nanoseconds resolution because
        modern filesystems and Box Drive's sync engine both record at
        ns precision; coarser ``st_mtime`` float would lose changes that
        happen inside the same second.
    fingerprint:
        ``f"{size}:{mtime_ns}"``. Colon-separated for human readability
        in ``opshub source show <id>`` output (ADR-0019 §決定 (d)
        rationale: hex hashes are opaque). The scanner sets this so all
        downstream code uses one canonical representation.
    """

    rel_path: str
    size: int
    mtime_ns: int
    fingerprint: str


class BoxDriveScanner:
    """Recursive, stat-only walker for a Box Drive mount point.

    The scanner enforces several structural guards beyond the
    no-``open()`` invariant:

    * **Symlink loops** — A symlink that ultimately points back into a
      parent directory would walk forever. We default to
      ``follow_symlinks=False`` (the safe choice for Box Drive, where
      symlinks have no native meaning). When the caller opts into
      following symlinks, an inode-based ``visited`` set deduplicates
      previously-walked directories.
    * **Depth blow-up** — A misconfigured root pointing at ``/`` would
      enumerate the entire filesystem. ``max_depth`` (default 16) is a
      structural cap; reaching it logs a warning and prunes the branch
      rather than raising, so a single misplaced deep tree does not
      fail the whole sync.
    * **File-count blow-up** — Box Drive workspaces can legitimately
      hold hundreds of thousands of files. ``max_files`` (default
      100,000) is an escape hatch that aborts the scan with a warning
      once exceeded. Operators with larger workspaces can raise the
      cap in ``opshub.toml``; raising it past ~1M should prompt a
      Phase 9.x chunked-scan discussion.
    * **Permission errors** — A directory the operator cannot read
      (e.g. another user's ``secrets/`` mount) is skipped with a log
      warning rather than aborting the entire scan. Box Drive itself
      occasionally surfaces EACCES on placeholder transitions, so
      tolerance here is operationally important.

    Failure semantics:

    * ``root_path`` missing or not a directory → :class:`ConfigError`
      (fail-fast; this is an operator misconfiguration, not a transient
      runtime issue).
    * Anything else (per-file ``OSError``, permission errors mid-walk)
      → logged warning, scan continues.
    """

    def __init__(
        self,
        root_path: Path,
        *,
        exclude_globs: list[str] | None = None,
        max_depth: int = 16,
        follow_symlinks: bool = False,
        max_files: int = 100_000,
    ) -> None:
        """Construct a scanner pinned to ``root_path``.

        Parameters
        ----------
        root_path:
            Absolute path to the Box Drive mount point. Must exist and
            be a directory at construction time, otherwise
            :class:`ConfigError` is raised before any walk begins.
        exclude_globs:
            Optional list of fnmatch-style glob patterns. A file is
            skipped if its ``rel_path`` matches any pattern. Patterns
            are matched in POSIX form (forward slashes) so
            ``"**/secrets/**"`` works identically on macOS / WSL2.
        max_depth:
            Hard cap on recursion depth measured from ``root_path``.
            Default 16 is generous for typical Box Drive workspaces
            (rarely more than 8 deep) and tight enough that a
            misconfigured root cannot enumerate ``/`` indefinitely.
        follow_symlinks:
            Default ``False`` — symlinks are observed as files (their
            stat is the link target's stat) but not descended.
            Setting this to ``True`` activates inode-tracking to break
            loops; the trade-off is exposing the scanner to anything
            the symlink target points at, which may live outside the
            Box Drive mount.
        max_files:
            Soft ceiling on the number of files yielded. When the cap
            is reached the scan logs a warning and returns; subsequent
            calls re-walk from scratch. Default 100,000.
        """
        if not root_path.exists():
            raise ConfigError(
                f"Box Drive root_path does not exist: {root_path}. "
                "See docs/box-drive-setup.md for WSL2 / macOS setup."
            )
        if not root_path.is_dir():
            raise ConfigError(f"Box Drive root_path is not a directory: {root_path}")

        self._root_path = root_path
        self._exclude_globs = list(exclude_globs) if exclude_globs else []
        self._max_depth = max_depth
        self._follow_symlinks = follow_symlinks
        self._max_files = max_files

    @property
    def root_path(self) -> Path:
        """Return the resolved root path the scanner was constructed with."""
        return self._root_path

    def scan(self, *, prior_fingerprints: dict[str, str]) -> Iterator[ScannedFile]:
        """Walk ``root_path`` and yield changed / new files.

        For each file encountered, the scanner computes
        ``fingerprint = f"{size}:{mtime_ns}"`` and compares it against
        ``prior_fingerprints[rel_path]``:

        * Match → skipped silently (no event noise).
        * Mismatch or absent → yielded as :class:`ScannedFile`.

        Removal is **not** detected (ADR-0019 §決定 (e)) — a file
        present in ``prior_fingerprints`` but missing from the walk is
        simply not yielded; downstream ``sources`` projection rows are
        left in place. Phase 9.x may surface them via
        ``opshub source list --stale``.

        Parameters
        ----------
        prior_fingerprints:
            ``{rel_path: fingerprint}`` map built by the connector from
            the ``sources`` projection. Empty dict is fine (first
            sync) — every file will be yielded.

        Yields
        ------
        ScannedFile
            One per file whose fingerprint differs from the prior map.

        Notes
        -----
        The generator is lazy: the caller can ``break`` early without
        forcing the rest of the tree to be walked. Each ``yield`` is
        therefore a natural transaction boundary for the connector's
        per-file ``SourceService.observe`` call.
        """
        visited: set[tuple[int, int]] = set()
        yielded = 0

        # Iterative DFS keeps the implementation single-function and
        # makes the max_depth / max_files caps trivially enforceable
        # (no recursion stack to overflow). Each stack entry is
        # (directory_path, depth).
        stack: list[tuple[Path, int]] = [(self._root_path, 0)]

        while stack:
            dir_path, depth = stack.pop()

            if depth > self._max_depth:
                _log.warning(
                    "box_drive.scan_max_depth_reached",
                    path=str(dir_path),
                    depth=depth,
                    max_depth=self._max_depth,
                )
                continue

            # Cycle break for follow_symlinks=True. ``stat`` (not
            # lstat) is correct here: we want the *target* inode so a
            # symlink loop closes back on the same entry. Wrap the
            # stat call so a vanished directory does not abort the
            # whole scan.
            if self._follow_symlinks:
                try:
                    st = dir_path.stat()
                except OSError as exc:
                    _log.warning(
                        "box_drive.scan_dir_stat_failed",
                        path=str(dir_path),
                        error=type(exc).__name__,
                    )
                    continue
                key = (st.st_dev, st.st_ino)
                if key in visited:
                    _log.warning(
                        "box_drive.scan_symlink_loop_break",
                        path=str(dir_path),
                    )
                    continue
                visited.add(key)

            try:
                # ``os.scandir`` returns DirEntry objects whose
                # ``.stat()`` result is cached in the iterator, so each
                # entry costs at most one syscall — far cheaper than
                # the pathlib equivalents on large trees.
                entries = list(os.scandir(dir_path))
            except PermissionError as exc:
                _log.warning(
                    "box_drive.scan_permission_denied",
                    path=str(dir_path),
                    error=type(exc).__name__,
                )
                continue
            except FileNotFoundError as exc:
                _log.warning(
                    "box_drive.scan_dir_missing",
                    path=str(dir_path),
                    error=type(exc).__name__,
                )
                continue
            except OSError as exc:
                _log.warning(
                    "box_drive.scan_dir_error",
                    path=str(dir_path),
                    error=type(exc).__name__,
                )
                continue

            for entry in entries:
                entry_path = Path(entry.path)

                # ``is_symlink`` does NOT follow; ``is_dir(follow_symlinks=False)``
                # and ``is_file(follow_symlinks=False)`` mirror that
                # explicit no-follow semantics.
                try:
                    is_symlink = entry.is_symlink()
                except OSError:
                    is_symlink = False

                if is_symlink and not self._follow_symlinks:
                    # Symlinks are skipped entirely when follow_symlinks
                    # is False — Box Drive does not synthesise links of
                    # its own, so any symlink under the root was created
                    # by the operator and is probably a deliberate
                    # escape hatch outside the workspace.
                    continue

                try:
                    is_dir = entry.is_dir(follow_symlinks=self._follow_symlinks)
                except OSError as exc:
                    _log.warning(
                        "box_drive.scan_entry_stat_failed",
                        path=entry.path,
                        error=type(exc).__name__,
                    )
                    continue

                if is_dir:
                    stack.append((entry_path, depth + 1))
                    continue

                try:
                    is_file = entry.is_file(follow_symlinks=self._follow_symlinks)
                except OSError as exc:
                    _log.warning(
                        "box_drive.scan_entry_stat_failed",
                        path=entry.path,
                        error=type(exc).__name__,
                    )
                    continue
                if not is_file:
                    # Sockets / fifos / block devices — Box Drive never
                    # creates these, so silently skip without log noise.
                    continue

                rel_path = entry_path.relative_to(self._root_path).as_posix()

                if self._is_excluded(rel_path):
                    continue

                try:
                    stat_result = entry.stat(follow_symlinks=self._follow_symlinks)
                except OSError as exc:
                    _log.warning(
                        "box_drive.scan_entry_stat_failed",
                        path=entry.path,
                        error=type(exc).__name__,
                    )
                    continue

                size = stat_result.st_size
                mtime_ns = stat_result.st_mtime_ns
                fingerprint = f"{size}:{mtime_ns}"

                # Diff detection — short-circuit unchanged files. Phase
                # 9 §決定 (d): identical fingerprint means we already
                # appended a SourceObserved for this rel_path at the
                # current (size, mtime_ns) so the connector skips it.
                if prior_fingerprints.get(rel_path) == fingerprint:
                    continue

                yielded += 1
                if yielded > self._max_files:
                    _log.warning(
                        "box_drive.scan_max_files_reached",
                        max_files=self._max_files,
                        root_path=str(self._root_path),
                    )
                    return

                yield ScannedFile(
                    rel_path=rel_path,
                    size=size,
                    mtime_ns=mtime_ns,
                    fingerprint=fingerprint,
                )

    def _is_excluded(self, rel_path: str) -> bool:
        """Return True when ``rel_path`` matches any configured exclude glob.

        Match semantics use :class:`pathlib.PurePosixPath.match` against
        the POSIX-form ``rel_path`` (forward slashes regardless of
        host). Python 3.13's matcher supports ``**`` recursion so
        gitignore-style patterns like ``"**/secrets/**"`` and
        ``"**/.git/**"`` work as operators expect.

        Two compatibility shims wrap the raw matcher:

        * Bare patterns without ``/`` (``".DS_Store"``, ``"Thumbs.db"``)
          are also tested against the basename so a top-level match
          catches the file regardless of nesting depth.
        * ``PurePosixPath.match`` requires ``**`` to consume at least
          one path segment, so a pattern like ``"**/secrets/**"`` does
          *not* match a top-level ``"secrets/key.pem"``. We therefore
          also test the path with a synthetic ``"./"`` prefix stripped
          form and against ``pattern`` rewritten without the leading
          ``**/`` so operators can use a single pattern shape for both
          nested and top-level cases (matching gitignore intuition).
        """
        if not self._exclude_globs:
            return False

        posix = PurePosixPath(rel_path)
        basename = posix.name
        for pattern in self._exclude_globs:
            if posix.match(pattern):
                return True
            if "/" not in pattern and PurePosixPath(basename).match(pattern):
                return True
            # Treat ``**/`` prefix as optional so ``**/secrets/**``
            # matches both ``secrets/key.pem`` and ``a/secrets/key.pem``.
            # This mirrors gitignore behaviour and is the least
            # surprising semantics for operators copying patterns from
            # ``.gitignore`` into ``opshub.toml``.
            if pattern.startswith("**/"):
                stripped = pattern.removeprefix("**/")
                if posix.match(stripped):
                    return True
        return False
