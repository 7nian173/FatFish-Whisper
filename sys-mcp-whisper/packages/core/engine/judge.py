"""Conflict judgement over per-field deltas (contract 4.2, plan-judge.md).

``is_conflict`` is a pure function: it never mutates the snapshot or the
deltas, never reads the clock, never touches IO.  Every rule lives in the
config as data (``ConflictConfig.rules``); this module contains no rule
name of its own - swapping the config swaps the behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ..domain.config import DEFAULT_ENGINE_CONFIG, EngineConfig
from ..domain.global_context import DELTA_ELIGIBLE_PATHS
from ..domain.state import S1Snapshot

#: Direction of one condition: strictly above / below epsilon.
Direction = Literal["up", "down"]


class HormoneDeltas(BaseModel):
    """Per-field effective change since the last evaluation anchor.

    Keys are dotted paths from ``global_context.DELTA_ELIGIBLE_PATHS`` (e.g.
    ``"pulse.dopamine"``, ``"ipc.affiliation"``); a missing key counts as 0.0
    (flat).  An *unknown* key (e.g. a typo like ``"dopamin"``, or the retired
    ``"hormones.dopamine"`` spelling) is rejected at construction time:
    silently treating it as "no change" would turn a real stimulus into a
    guaranteed false negative.

    ``model_config`` notes (plan-judge.md R2):
    - ``frozen=True`` makes field assignment raise ``ValidationError``;
    - ``extra="forbid"`` only guards the model's own top-level field names
      (e.g. a misspelled future field); dict keys inside ``values`` are
      validated by ``_keys_must_be_valid_paths`` below, not by ``extra``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: dict[str, float]

    @field_validator("values")
    @classmethod
    def _keys_must_be_valid_paths(cls, v: dict[str, float]) -> dict[str, float]:
        valid = DELTA_ELIGIBLE_PATHS
        for k in v:
            if k not in valid:
                raise ValueError(f"unknown delta key: {k}")
        return v


@dataclass(frozen=True)
class ConflictVerdict:
    """Outcome of ``is_conflict``: the boolean plus the matched rule names."""

    conflicted: bool
    matched_rules: tuple[str, ...]


def is_conflict(
    curr: S1Snapshot,
    deltas: HormoneDeltas,
    config: EngineConfig = DEFAULT_ENGINE_CONFIG,
) -> ConflictVerdict:
    """Evaluate the rule table in ``config.conflict`` against ``deltas``.

    Semantics (plan-config 2.2): ``delta > epsilon`` is up, ``delta < -epsilon``
    is down, ``|delta| <= epsilon`` is flat (strict inequalities, no
    ``math.isclose`` - epsilon already carries the tolerance).  Conditions
    inside one rule combine with AND (short-circuit), rules combine with OR
    (no short-circuit: every hit is recorded).  ``matched_rules`` is sorted by
    ascending ``priority``.  A rule whose condition path is not in
    ``DELTA_ELIGIBLE_PATHS`` raises :class:`ValueError` before evaluation.
    """
    cfg = config.conflict

    valid_paths = DELTA_ELIGIBLE_PATHS
    for rule in cfg.rules:
        for path, _direction in rule.conditions:
            if path not in valid_paths:
                raise ValueError(f"invalid field_path: {path}")

    matched: list[str] = []
    for rule in sorted(cfg.rules, key=lambda r: r.priority):
        ok = True
        for path, direction in rule.conditions:
            delta = deltas.values.get(path, 0.0)
            if direction == "up":
                ok = ok and (delta > cfg.epsilon)
            elif direction == "down":
                ok = ok and (delta < -cfg.epsilon)
            if not ok:
                break  # AND short-circuit inside one rule
        if ok:
            matched.append(rule.name)  # OR between rules: record every hit

    return ConflictVerdict(conflicted=bool(matched), matched_rules=tuple(matched))
