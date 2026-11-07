"""H1-H6 locks for ``update_hormones`` (task-FFW-P3-E, ADR-008 section 1.7).

``update_hormones`` fans one dotted-path delta out across two carriers: the
``pulse.*`` keys land on ``PulseState`` (global, shared by every scope) and the
``ipc.*`` keys land on ``S1Snapshot.ipc`` (per user).  That split is the whole
reason the function returns a pair -- a caller cannot forget one carrier,
because there is no way to receive only one.

Both carriers are clamped explicitly with ``max()``/``min()`` before the model
round trip.  Relying on the Pydantic bounds instead would turn a 103.0 delta
into a ``ValidationError`` at the boundary, so a stimulus that arrives when a
hormone already sits near its rail would abort the request instead of pinning
the value.

``apply_limit_cap`` is deliberately not called here (human ruling 2026-11-07
10:05): that function limits a *stimulus* through the single/window/daily
layers, and applies at most +/-1 per event.  ``update_hormones`` applies an
already-limited delta, so capping again would silently shrink it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.core.domain.global_context import PulseState
from packages.core.domain.state import (
    EmotionValue,
    IPCState,
    S1Snapshot,
    TraitState,
)
from packages.core.engine.dynamics import update_hormones
from packages.core.engine.judge import HormoneDeltas

TS = datetime(2026, 11, 7, 2, 0, 0)
TTL = datetime(2030, 1, 1, tzinfo=UTC)


def make_snapshot(
    *,
    affiliation: float = 0.0,
    dominance: float = 12.5,
    version: int = 4,
    tier: str = "hot",
    is_conflict: bool = True,
) -> S1Snapshot:
    """A fully populated local snapshot; only ``ipc`` is meant to move."""
    return S1Snapshot(
        version=version,
        scope_id="scope-h",
        timestamp=TS,
        last_active_at=TS,
        last_decay_at=TS,
        is_conflict=is_conflict,
        compute_tier=tier,
        ipc=IPCState(
            affiliation=affiliation,
            dominance=dominance,
            relationship_stage="friend",
            relationship_ceiling="lover",
        ),
        traits={"warmth": TraitState(score=70, visibility="unlocked")},
        emotions={"anger": EmotionValue(intensity=20.0, ttl_expires_at=TTL)},
    )


def make_pulse(
    *,
    dopamine: float = 50.0,
    cortisol: float = 50.0,
    oxytocin: float = 50.0,
    delayed_impact: dict[str, float] | None = None,
) -> PulseState:
    return PulseState(
        dopamine=dopamine,
        cortisol=cortisol,
        oxytocin=oxytocin,
        delayed_impact=delayed_impact if delayed_impact is not None else {},
    )


def test_a_pulse_delta_lands_on_the_pulse_carrier_only() -> None:
    """H1: dopamine 50 + 5 = 55, and the snapshot half is untouched."""
    snapshot = make_snapshot()
    pulse = make_pulse(dopamine=50.0)

    new_snapshot, new_pulse = update_hormones(
        snapshot, pulse, HormoneDeltas(values={"pulse.dopamine": 5.0})
    )

    assert new_pulse.dopamine == 55.0
    assert new_snapshot.ipc.affiliation == snapshot.ipc.affiliation
    assert new_pulse.cortisol == 50.0
    assert new_pulse.oxytocin == 50.0


def test_a_hormone_above_the_ceiling_clamps_to_exactly_one_hundred() -> None:
    """H2: 98 + 5 pins at 100.0 instead of raising from the model bounds."""
    _snapshot, new_pulse = update_hormones(
        make_snapshot(),
        make_pulse(dopamine=98.0),
        HormoneDeltas(values={"pulse.dopamine": 5.0}),
    )

    assert new_pulse.dopamine == 100.0


def test_a_hormone_below_the_floor_clamps_to_exactly_zero() -> None:
    """H3: 2 - 5 pins at 0.0."""
    _snapshot, new_pulse = update_hormones(
        make_snapshot(),
        make_pulse(cortisol=2.0),
        HormoneDeltas(values={"pulse.cortisol": -5.0}),
    )

    assert new_pulse.cortisol == 0.0


def test_both_ipc_coordinates_accept_signed_increments() -> None:
    """H4: -10 and +10 move respectively, in the -100..100 band."""
    new_snapshot, _pulse = update_hormones(
        make_snapshot(affiliation=10.0, dominance=5.0),
        make_pulse(),
        HormoneDeltas(values={"ipc.affiliation": -10.0, "ipc.dominance": 10.0}),
    )

    assert new_snapshot.ipc.affiliation == 0.0
    assert new_snapshot.ipc.dominance == 15.0


def test_ipc_clamps_on_both_rails() -> None:
    """H5: the positive rail is 100.0 and the negative rail is -100.0.

    A one-sided clamp -- ``min(100.0, ...)`` with no ``max(-100.0, ...)`` -- would
    pass the positive half of this test and let the negative half run to -105.0,
    where the Pydantic bound would then raise instead of pinning.
    """
    high_snapshot, _ = update_hormones(
        make_snapshot(affiliation=95.0),
        make_pulse(),
        HormoneDeltas(values={"ipc.affiliation": 10.0}),
    )
    low_snapshot, _ = update_hormones(
        make_snapshot(affiliation=-95.0),
        make_pulse(),
        HormoneDeltas(values={"ipc.affiliation": -10.0}),
    )

    assert high_snapshot.ipc.affiliation == 100.0
    assert low_snapshot.ipc.affiliation == -100.0


def test_every_non_numeric_field_survives_the_round_trip() -> None:
    """H6: only the two IPC numbers may move; everything else is verbatim.

    Each field is asserted separately on purpose: a rebuild that dropped one
    field would fall back to that field's Pydantic default and the comparison
    of a whole ``model_dump()`` dict would point at the wrong culprit.
    """
    snapshot = make_snapshot(affiliation=10.0)
    pulse = make_pulse()

    new_snapshot, _new_pulse = update_hormones(
        snapshot, pulse, HormoneDeltas(values={"ipc.affiliation": 5.0})
    )

    assert new_snapshot.version == snapshot.version
    assert new_snapshot.scope_id == snapshot.scope_id
    assert new_snapshot.timestamp == snapshot.timestamp
    assert new_snapshot.last_active_at == snapshot.last_active_at
    assert new_snapshot.last_decay_at == snapshot.last_decay_at
    assert new_snapshot.is_conflict == snapshot.is_conflict
    assert new_snapshot.compute_tier == snapshot.compute_tier
    assert new_snapshot.traits == snapshot.traits
    assert new_snapshot.emotions == snapshot.emotions
    assert new_snapshot.ipc.relationship_stage == snapshot.ipc.relationship_stage
    assert new_snapshot.ipc.relationship_ceiling == snapshot.ipc.relationship_ceiling


def test_update_hormones_does_not_mutate_its_arguments() -> None:
    """H6b: both inputs are byte-identical after the call."""
    snapshot = make_snapshot(affiliation=10.0)
    pulse = make_pulse(delayed_impact={"dopamine": 3.0})
    deltas = HormoneDeltas(values={"ipc.affiliation": 5.0, "pulse.dopamine": 2.0})
    snapshot_before = snapshot.model_dump_json()
    pulse_before = pulse.model_dump_json()
    deltas_before = deltas.model_dump_json()

    update_hormones(snapshot, pulse, deltas)

    assert snapshot.model_dump_json() == snapshot_before
    assert pulse.model_dump_json() == pulse_before
    assert deltas.model_dump_json() == deltas_before


def test_the_two_results_are_new_objects() -> None:
    """H1b: a pair of new carriers, not aliases of the inputs."""
    snapshot = make_snapshot()
    pulse = make_pulse()

    new_snapshot, new_pulse = update_hormones(
        snapshot, pulse, HormoneDeltas(values={"pulse.oxytocin": 1.0})
    )

    assert new_snapshot is not snapshot
    assert new_pulse is not pulse
    assert new_snapshot.ipc is not snapshot.ipc


def test_delayed_impact_is_copied_not_aliased() -> None:
    """H4b: the pending-increment map is carried over as a copy.

    A shared dict would let a later in-place edit of the returned pulse reach
    back into the caller's pulse, which is the aliasing bug the ``dict(...)``
    call exists to prevent.
    """
    pulse = make_pulse(delayed_impact={"dopamine": 3.0})

    _snapshot, new_pulse = update_hormones(
        make_snapshot(), pulse, HormoneDeltas(values={"pulse.dopamine": 1.0})
    )

    assert new_pulse.delayed_impact == {"dopamine": 3.0}
    assert new_pulse.delayed_impact is not pulse.delayed_impact

    new_pulse.delayed_impact["dopamine"] = 99.0
    assert pulse.delayed_impact == {"dopamine": 3.0}


def test_a_delta_naming_nothing_is_a_no_op() -> None:
    """H0: an empty delta hands back equal values in fresh objects."""
    snapshot = make_snapshot(affiliation=33.0)
    pulse = make_pulse(dopamine=61.0)

    new_snapshot, new_pulse = update_hormones(
        snapshot, pulse, HormoneDeltas(values={})
    )

    assert new_snapshot.model_dump() == snapshot.model_dump()
    assert new_pulse.model_dump() == pulse.model_dump()
    assert new_snapshot is not snapshot
    assert new_pulse is not pulse


def test_a_mixed_delta_moves_both_carriers_at_once() -> None:
    """H1c: one call, both halves -- the reason the signature returns a pair."""
    new_snapshot, new_pulse = update_hormones(
        make_snapshot(affiliation=20.0),
        make_pulse(oxytocin=40.0),
        HormoneDeltas(
            values={"pulse.oxytocin": 7.0, "ipc.affiliation": 4.0}
        ),
    )

    assert new_pulse.oxytocin == 47.0
    assert new_snapshot.ipc.affiliation == 24.0


@pytest.mark.parametrize(
    ("key", "start", "delta", "expected"),
    [
        ("pulse.dopamine", 0.0, -1.0, 0.0),
        ("pulse.cortisol", 100.0, 1.0, 100.0),
        ("pulse.oxytocin", 99.5, 0.5, 100.0),
        ("ipc.affiliation", 100.0, 1.0, 100.0),
        ("ipc.dominance", -100.0, -1.0, -100.0),
    ],
)
def test_every_whitelist_path_is_clamped_at_its_own_rail(
    key: str, start: float, delta: float, expected: float
) -> None:
    """H5b: each of the five delta paths pins at its own boundary.

    ``ipc.*`` is -100..100 while ``pulse.*`` is 0..100, so a shared clamp
    constant would be wrong for one family.  Parametrising over all five keeps
    a future whitelist change from silently skipping the new member.
    """
    if key.startswith("ipc."):
        field = key.split(".", 1)[1]
        snapshot = make_snapshot(
            affiliation=start if field == "affiliation" else 0.0,
            dominance=start if field == "dominance" else 0.0,
        )
        new_snapshot, _pulse = update_hormones(
            snapshot, make_pulse(), HormoneDeltas(values={key: delta})
        )
        actual = getattr(new_snapshot.ipc, field)
    else:
        field = key.split(".", 1)[1]
        pulse = make_pulse(**{field: start})
        _snapshot, new_pulse = update_hormones(
            make_snapshot(), pulse, HormoneDeltas(values={key: delta})
        )
        actual = getattr(new_pulse, field)

    assert actual == expected
