"""Domain layer.

Pure business types: event definitions, aggregates, value objects. The domain
layer may depend only on :mod:`opshub.core` (one-way dependency, ADR-0002).
No I/O, no SQLAlchemy, no Pydantic Settings.
"""
