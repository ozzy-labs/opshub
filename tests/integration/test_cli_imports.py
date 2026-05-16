"""Static guard against heavy module-level imports in ``opshub.cli.*``.

ADR-0001 sets a ~300ms target for ``opshub --help`` cold start. Each
subcommand module keeps that promise by deferring its heavy imports
(``sqlalchemy``, ``alembic``, ``pydantic_settings``, ``jinja2``,
``structlog``, plus the inner ``opshub.{core,db,domain,services,
projections,markdown,vectors}`` packages) into the command callback.
The Phase 1 integration tripwire (``tests/integration/test_cold_start``)
asserts the wall-clock budget; this companion test asserts the **cause**
by parsing every ``opshub/cli/*.py`` with :mod:`ast` and flagging any
forbidden top-level import.

The static check is intentionally faster and louder than the cold-start
test: a regressing PR sees the offending file + import surfaced in the
assertion message, instead of a flaky wall-clock failure.

Allow-list philosophy:

* ``__future__``, plain stdlib types (``pathlib``, ``typing``, ...) and
  ``typer`` are always fine — they are tiny and Typer is already paid
  for by the entrypoint.
* ``opshub`` (the bare package — i.e. ``from opshub import __version__``)
  is fine because its ``__init__`` is a one-liner.
* ``opshub.cli`` siblings are fine: the registration step in
  ``cli/app.py`` is *supposed* to import each sub-app at module load.
  Sub-app modules themselves stay tiny (only ``typer`` + ``__future__``).
* Everything else under ``opshub.*`` (notably ``opshub.core``,
  ``opshub.db``, ``opshub.domain``, ``opshub.services``,
  ``opshub.projections``, ``opshub.markdown``, ``opshub.vectors``) must
  be imported *inside* the command callback so cold start does not pay
  for them.
* Heavy third-party packages (``sqlalchemy``, ``alembic``,
  ``pydantic_settings``, ``jinja2``, ``structlog``) are forbidden
  outright at module level — they are precisely what the lazy-import
  rule was written for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Directly forbidden third-party modules (or top-level prefixes thereof).
# These almost never have a legitimate reason to be imported at module level
# inside a CLI command file.
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "sqlalchemy",
    "alembic",
    "pydantic_settings",
    "jinja2",
    "structlog",
)

# Forbidden ``opshub.*`` subpackages. Importing any of these at module
# level pulls in heavy transitive deps (sqlalchemy, pydantic, etc.) and
# defeats the ADR-0001 cold-start budget.
_FORBIDDEN_OPSHUB_SUBPKGS: tuple[str, ...] = (
    "opshub.core",
    "opshub.db",
    "opshub.domain",
    "opshub.services",
    "opshub.projections",
    "opshub.markdown",
    "opshub.vectors",
)

# Stdlib modules that are cheap and idiomatic to import at module level
# (e.g. for ``TYPE_CHECKING`` blocks or class decorators).
_STDLIB_OK: frozenset[str] = frozenset(
    {
        "__future__",
        "typing",
        "pathlib",
        "collections",
        "collections.abc",
        "dataclasses",
        "enum",
        "functools",
        "itertools",
        "os",
        "sys",
    }
)


def _cli_files() -> list[Path]:
    """Return the public ``.py`` files under ``src/opshub/cli`` that the check covers.

    The check only inspects **public** modules — i.e. those that get
    imported at module load time by ``cli/app.py`` for command
    registration. Private helpers (``_wiring.py``, ``_task_list.py``,
    etc.) live behind the ``_`` prefix specifically because they are
    only reached through a lazy import inside a command callback; the
    cold-start budget never pays for them, so they are free to use
    SQLAlchemy / pydantic etc. at module level.

    ``__init__.py`` is also skipped — it is empty in this package and
    has no imports to police.
    """
    # Anchor on this test file's location so the test runs identically
    # whether invoked from the repo root or via ``uv run pytest``.
    repo_root = Path(__file__).resolve().parents[2]
    cli_dir = repo_root / "src" / "opshub" / "cli"
    assert cli_dir.is_dir(), f"expected {cli_dir} to exist"
    return sorted(p for p in cli_dir.glob("*.py") if p.is_file() and not p.name.startswith("_"))


def _is_allowed(module_name: str, *, source_file: str) -> bool:
    """Return True iff ``module_name`` is acceptable as a top-level import.

    Parameters
    ----------
    module_name:
        The dotted name being imported (``import X`` → ``X``;
        ``from X import Y`` → ``X``).
    source_file:
        The basename of the importing file. Used to special-case
        ``cli/app.py``, which is *supposed* to import each sub-app at
        module level — that is its job as the registration point.
    """
    # ``typer`` is the foundation of every CLI module; always fine.
    if module_name == "typer" or module_name.startswith("typer."):
        return True

    # Stdlib short-list. ``module_name.split(".")[0]`` lets nested
    # ``collections.abc`` match the bare ``collections`` entry.
    head = module_name.split(".", 1)[0]
    if module_name in _STDLIB_OK or head in _STDLIB_OK:
        return True

    # ``from opshub import __version__`` — the bare package is fine
    # because ``opshub/__init__.py`` is a one-line ``__version__``.
    if module_name == "opshub":
        return True

    # Sibling ``opshub.cli`` imports — but ONLY inside ``cli/app.py``,
    # which is the registration point. Other CLI modules must not pull
    # in siblings at module level (each command keeps its imports
    # inside the callback so unused commands don't load their deps).
    if module_name.startswith("opshub.cli"):
        return source_file == "app.py"

    # Anything else under ``opshub.*`` is forbidden at module level.
    if module_name.startswith("opshub."):
        return False

    # Explicitly forbidden third-party heavy hitters.
    if any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_PREFIXES
    ):
        return False

    # Anything else (unknown third-party) — be conservative and reject.
    # This catches future regressions where a contributor reaches for a
    # new heavyweight dep without thinking about cold start.
    return False


def _is_type_checking_guard(node: ast.If) -> bool:
    """True iff ``node`` is an ``if TYPE_CHECKING:`` (or ``if typing.TYPE_CHECKING:``) block.

    These guards do not execute at runtime, so anything imported under
    them is invisible to the cold-start path. Skipping them keeps the
    check purely about real, executed imports.
    """
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    ):
        return True
    return False


def _collect_top_level_imports(tree: ast.Module) -> list[tuple[str, int]]:
    """Return ``(module_name, lineno)`` for every **executed** top-level import.

    What counts as "executed":

    * ``import X`` and ``from X import Y`` at module scope.

    What is **excluded**:

    * Imports nested inside a function / method / class body — those are
      the very lazy imports the rule rewards.
    * Imports inside an ``if TYPE_CHECKING:`` guard — those exist solely
      for the type checker, never for the runtime, so they cannot
      contribute to cold start.
    """
    found: list[tuple[str, int]] = []
    for node in tree.body:  # top-level only — function bodies live in child nodes
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (``from . import x``) resolve to the
            # current package; treat them as siblings of the file.
            if node.level and not node.module:
                # ``from . import X`` — synthesise an ``opshub.cli`` name
                # for the allowlist check.
                found.append(("opshub.cli", node.lineno))
                continue
            module = node.module or ""
            if node.level:
                # Relative ``from .sub import X`` — also sibling.
                module = f"opshub.cli.{module}"
            found.append((module, node.lineno))
        # Any other top-level statement (including ``if TYPE_CHECKING:``)
        # is irrelevant: imports nested inside it are not executed at
        # module load. We deliberately do NOT recurse here.
        elif isinstance(node, ast.If) and not _is_type_checking_guard(node):
            # A non-TYPE_CHECKING ``if`` at module level would still
            # execute its body on import — flag every import inside as
            # if it were top-level. (No file in opshub currently uses
            # this pattern, but the check should not silently ignore
            # it.)
            for child in node.body:
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        found.append((alias.name, child.lineno))
                elif isinstance(child, ast.ImportFrom):
                    module = child.module or ""
                    if child.level:
                        module = f"opshub.cli.{module}" if module else "opshub.cli"
                    found.append((module, child.lineno))
    return found


@pytest.mark.parametrize("path", _cli_files(), ids=lambda p: p.name)
def test_cli_module_has_no_heavy_top_level_imports(path: Path) -> None:
    """Every ``opshub/cli/*.py`` may only import allowlisted modules at the top level.

    The test parses the file with :mod:`ast` so it does not actually
    execute the module — false negatives from import-time side effects
    are impossible. Each forbidden import is reported as a single
    assertion failure listing the file, the offending module, and the
    line number; that is enough for an IDE jump-to-source on regression.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = [
        f"{path.name}:{lineno} imports {module!r} at module level"
        for module, lineno in _collect_top_level_imports(tree)
        if not _is_allowed(module, source_file=path.name)
    ]
    assert not violations, (
        "Forbidden module-level imports found in opshub.cli (ADR-0001 "
        "lazy-import rule). Move these imports inside the command "
        "callback:\n  - " + "\n  - ".join(violations)
    )
