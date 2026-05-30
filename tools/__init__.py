"""Development / maintenance tools for the opshub repo (not packaged).

Modules here are imported by ``tests/`` and ``ozzy-labs/skills`` CI but
are **not** shipped with ``uv tool install opshub``. They live outside
``src/opshub/`` so the cold-start guard (ADR-0006, M6 budget) is not
affected.
"""
