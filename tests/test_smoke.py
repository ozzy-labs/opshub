"""Bootstrap smoke tests."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from opshub import __version__
from opshub.cli.app import app

# release-please does not update test fixtures via ``extra-files``, so
# asserting a hardcoded version string would silently rot after every
# release PR merge (encountered during v0.2.0). Pin to the shape of the
# value instead — the tag-vs-package equality check lives in
# ``release-please.yaml``'s publish job.
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def test_version_constant_is_set() -> None:
    assert __version__
    assert _SEMVER_RE.match(__version__), (
        f"__version__ should be SemVer-shaped: {__version__!r}"
    )


def test_cli_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
