# OpsHub task runner. Run `just` (no args) to list recipes.
#
# Recipes are thin wrappers over `uv run ...` so that CI / lefthook / IDE all
# share the same commands. Heavy logic stays out of justfile.

set shell := ["bash", "-cu"]
set dotenv-load := false

# Default recipe: show the available recipes.
default:
    @just --list

# Install project + dev dependencies.
install:
    uv sync --extra dev

# Install everything (vector + local embedding + connectors + dev). Heavy.
install-all:
    uv sync --all-extras

# Auto-fix lint and format Python files.
lint:
    uv run ruff check --fix
    uv run ruff format

# Verify lint without modifying files (CI-friendly).
lint-check:
    uv run ruff check
    uv run ruff format --check

# Fast type check (pyright, local default).
typecheck:
    uv run pyright

# Strict type check (mypy, CI mirror).
typecheck-strict:
    uv run mypy src tests

# Run tests.
test:
    uv run pytest

# Run tests in parallel with coverage.
test-cov:
    uv run pytest -n auto --cov=opshub --cov-report=term-missing

# Run the opshub CLI through uv (e.g. `just opshub version`).
opshub *args:
    uv run opshub {{args}}

# Full CI mirror. Mirrors what .github/workflows/ci.yaml will run.
#
# The ``connectors-github`` extras is included so that type-checkers
# (pyright, mypy) and pytest can resolve ``httpx`` -- which is required
# by ``opshub.connectors.github.api`` (Phase 3 step B2). The cold-start
# guard test still asserts that ``httpx`` does NOT leak onto the
# ``opshub --help`` path (see ``tests/integration/test_cli_imports.py``).
#
# ``vector`` is included so Phase 4 migration 0013 (CREATE VIRTUAL TABLE
# ... USING vec0) can run during the migration integration tests. The
# extras pulls in ``sqlite-vec`` + ``numpy``; the engine's connect
# listener (``opshub.db.engine``) loads sqlite-vec automatically.
#
# ``llm-anthropic`` is included so Phase 5 step A3 unit tests
# (``tests/unit/llm/test_anthropic_client.py``) can ``importorskip``
# the ``anthropic`` SDK and run mocked round-trips. No real API call
# leaves CI — every SDK touchpoint is patched (see ADR-0015 §決定 + the
# Phase 4 OpenAI embedder precedent).
#
# ``llm-openai`` is included so Phase 5 step A4 tests
# (``tests/unit/llm/test_openai_client.py``) can import the ``openai``
# SDK; the tests still mock every network call (``openai.OpenAI`` is
# patched per-test) so no real chat-completion request leaves the
# process.
#
# ``llm-ollama`` is included so Phase 6 step A4 tests
# (``tests/unit/llm/test_ollama_client.py``) can import ``httpx`` and
# exercise the Ollama OpenAI-compatible client; every HTTP request is
# routed through ``httpx.MockTransport`` so no real daemon call leaves
# CI. ``httpx`` is already pulled by ``connectors-github`` so the
# additional ``--extra`` flag is informational — it pins the dependency
# even if the GitHub connector extras shift in the future.
#
# ``connectors-box`` is included so Phase 7 step C1 unit tests
# (``tests/unit/connectors/box/test_auth.py``) can ``importorskip``
# the ``boxsdk`` SDK and exercise the OAuth paste-code flow. Every
# OAuth call is patched at the ``boxsdk.OAuth2`` boundary so no real
# Box API request leaves CI (Phase 7 plan §1 #6 + the Phase 3 GitHub
# connector mocking precedent). The cold-start guard still asserts
# ``boxsdk`` never leaks onto the ``opshub --help`` path.
ci:
    uv sync --locked --extra dev --extra connectors-github --extra connectors-box --extra vector --extra llm-anthropic --extra llm-openai --extra llm-ollama
    uv run ruff check
    uv run ruff format --check
    uv run pyright
    uv run mypy src tests
    uv run pytest

# Remove Python tooling caches.
clean:
    rm -rf .pytest_cache .ruff_cache .mypy_cache .pyright dist build
    find . -type d -name __pycache__ -prune -exec rm -rf {} +
