"""JARVIS Core - the deterministic brain (SPEC §5, ADR-0001/0002).

Everything that has side effects runs through Permission Engine -> Execution Gateway -> Verifier
and emits typed events on the Event Bus. Model providers live behind interfaces in core.models.
"""

__version__ = "1.0.0rc1"
