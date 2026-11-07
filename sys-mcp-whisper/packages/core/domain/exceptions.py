"""Exceptions shared by the domain layer (contract 4.1 time-decay semantics)."""

from __future__ import annotations


class TimeDecayError(Exception):
    """Raised by engine.dynamics.apply_time_decay when delta_t is negative.

    delta_t == 0 does not raise: the snapshot is returned with every field
    unchanged (decision R1, option A).
    """
