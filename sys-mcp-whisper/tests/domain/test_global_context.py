"""Contract tests for the P2.8 global context skeleton (ADR-008).

The whole file is written before ``packages/core/domain/global_context.py``
exists, so the first run fails at collection (ImportError) and only turns green
once the six models and the six placeholder constants are in place.

Scope is structural only: the card builds the contract and nothing else.  No
engine file may consume these models yet (``apply_deltas`` is C3), so there is
deliberately no test here that touches ``dynamics``/``router``/``assembler``.

The last three cases are the anti-drift locks: the persona vocabulary must equal
the placeholder-modifier key set, the modifier mapping must refuse writes, and
every placeholder constant must carry the mandated comment line above it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.domain import config as config_module
from packages.core.domain.config import PLACEHOLDER_PERSONA_HEAT_MODIFIER
from packages.core.domain.global_context import (
    BaselineState,
    GlobalContext,
    GlobalDelta,
    PulseState,
    SuppressionDelta,
    TensionState,
)

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "packages" / "core" / "domain"
CONFIG_SOURCE = (DOMAIN_DIR / "config.py").read_text(encoding="utf-8")
CONFIG_LINES = CONFIG_SOURCE.splitlines()

#: The mandated sentence, verbatim (human ruling 2026-11-06).
#:
#: The full-width comma is spelled with an escape rather than pasted, so this
#: module needs no ruff suppression: the runtime string is still character-for
#: -character the ruling's sentence, which is what the six assertions compare.
PLACEHOLDER_COMMENT = "此为结构占位值\uff0c实际业务数值待 P4 阶段基于真实对话数据压测后裁决。"

#: The names the cards mandate, written out so a deletion is visible here.
#: PLACEHOLDER_STIMULUS_UNIT_POINTS was added by task-FFW-P3-LIMIT (ADR-009
#: S5: the points<->units bridge), so the count is now seven.
EXPECTED_PLACEHOLDER_NAMES = frozenset(
    {
        "PLACEHOLDER_RESIDENT_BONUS_RATE",
        "PLACEHOLDER_IPC_BONUS_RATE",
        "PLACEHOLDER_TENSION_THRESHOLD",
        "PLACEHOLDER_PERSONA_HEAT_MODIFIER",
        "PLACEHOLDER_MAX_QUEUE_RETRIES",
        "PLACEHOLDER_RESIDENT_RESET_DAYS",
        "PLACEHOLDER_STIMULUS_UNIT_POINTS",
    }
)

#: Discovered, not hardcoded a second time: ``dir()`` on the module is what a
#: future constant would actually have to pass through. The equality assertion
#: below keeps the two lists honest in both directions.
PLACEHOLDER_NAMES = tuple(
    sorted(name for name in dir(config_module) if name.startswith("PLACEHOLDER_"))
)


def test_the_six_placeholder_constants_exist() -> None:
    """Guard the discovery above: an empty scan must fail, not pass silently."""
    assert set(PLACEHOLDER_NAMES) == EXPECTED_PLACEHOLDER_NAMES, (
        f"占位常量集合漂移: 实际 {sorted(PLACEHOLDER_NAMES)}"
    )


# ---------------------------------------------------------------------------
# 1-4: PulseState
# ---------------------------------------------------------------------------


def test_pulse_state_defaults() -> None:
    """A fresh pulse sits on the shared baseline and has no pending impact."""
    pulse = PulseState()

    assert pulse.dopamine == 50.0
    assert pulse.cortisol == 50.0
    assert pulse.oxytocin == 50.0
    assert pulse.delayed_impact == {}


#: The three pulse axes are three independent fields with three independent
#: ``Field(ge/le)`` pairs.  Locking ``dopamine`` proves nothing about the
#: other two, so every axis is exercised on both sides of both bounds.
_PULSE_AXES = ("dopamine", "cortisol", "oxytocin")


@pytest.mark.parametrize("axis", _PULSE_AXES)
@pytest.mark.parametrize("value", [0.0, 100.0])
def test_pulse_state_accepts_both_inclusive_bounds(axis: str, value: float) -> None:
    """0.0 and 100.0 are inside the scale, so they must construct."""
    pulse = PulseState(**{axis: value})

    assert getattr(pulse, axis) == value
    # The other two axes must be untouched by the override.
    for other in _PULSE_AXES:
        if other != axis:
            assert getattr(pulse, other) == 50.0


@pytest.mark.parametrize("axis", _PULSE_AXES)
@pytest.mark.parametrize(
    ("value", "error_type"),
    [(-1.0, "greater_than_equal"), (101.0, "less_than_equal")],
)
def test_pulse_state_rejects_out_of_range_hormones(
    axis: str, value: float, error_type: str
) -> None:
    """Both ends of the 0..100 scale are enforced, on every axis."""
    with pytest.raises(ValidationError) as excinfo:
        PulseState(**{axis: value})

    errors = excinfo.value.errors()
    # Assert the error *type*, not just the field: a typo'd keyword would
    # land on the same loc via extra_forbidden and look like a real reject.
    assert [error["type"] for error in errors] == [error_type]
    assert errors[0]["loc"] == (axis,)


def test_pulse_state_rejects_unknown_delayed_impact_key() -> None:
    """``extra="forbid"`` cannot see dict keys, so the validator must."""
    with pytest.raises(ValueError, match="unknown delayed_impact key"):
        PulseState(delayed_impact={"dopamin": 1.0})


def test_pulse_state_rejects_out_of_range_delayed_impact_value() -> None:
    """Per-item bounds: ``Field(ge/le)`` does not reach inside a dict."""
    with pytest.raises(ValueError, match="out of range"):
        PulseState(delayed_impact={"dopamine": 101.0})

    assert PulseState(delayed_impact={"dopamine": 100.0}).delayed_impact == {"dopamine": 100.0}


# ---------------------------------------------------------------------------
# 5-6: GlobalDelta
# ---------------------------------------------------------------------------


def test_global_delta_uses_dotted_pulse_paths() -> None:
    """Keys are dotted whitelist paths; C4 retired the bare spelling."""
    assert GlobalDelta(pulse={"pulse.dopamine": 1.0}).pulse == {"pulse.dopamine": 1.0}

    with pytest.raises(ValueError, match="unknown pulse key"):
        GlobalDelta(pulse={"pulse.dopamin": 1.0})


def test_global_delta_is_frozen() -> None:
    """A submitted delta is an immutable receipt."""
    delta = GlobalDelta(pulse={"pulse.dopamine": 1.0})

    with pytest.raises(ValidationError):
        delta.pulse = {}  # type: ignore[misc]
    assert delta.pulse == {"pulse.dopamine": 1.0}


# ---------------------------------------------------------------------------
# 7-8: placeholder constants vs. the model vocabulary
# ---------------------------------------------------------------------------


def test_persona_vocabulary_matches_the_modifier_keys() -> None:
    """Two declarations of the same vocabulary must not drift apart."""
    literal_args = set(BaselineState.model_fields["persona"].annotation.__args__)

    assert set(PLACEHOLDER_PERSONA_HEAT_MODIFIER) == literal_args
    assert set(PLACEHOLDER_PERSONA_HEAT_MODIFIER) == {"cold", "warm", "hot"}


def test_persona_heat_modifier_is_immutable() -> None:
    """A mutable module-level mapping would let any caller rewrite behaviour."""
    with pytest.raises(TypeError):
        PLACEHOLDER_PERSONA_HEAT_MODIFIER["hot"] = 99  # type: ignore[index]

    assert PLACEHOLDER_PERSONA_HEAT_MODIFIER["hot"] == 1.5


@pytest.mark.parametrize("constant_name", PLACEHOLDER_NAMES)
def test_every_placeholder_constant_carries_the_mandated_comment(constant_name: str) -> None:
    """The sentence must sit within the three lines above the declaration."""
    pattern = re.compile(rf"^{constant_name}\s*:")
    indices = [i for i, line in enumerate(CONFIG_LINES) if pattern.match(line)]

    assert len(indices) == 1, f"{constant_name} 应恰好声明一次; 实际 {len(indices)} 次"
    index = indices[0]
    window = CONFIG_LINES[max(0, index - 3) : index]
    assert any(PLACEHOLDER_COMMENT in line for line in window), (
        f"{constant_name} 上方三行内缺少规定注释句; 实际窗口: {window}"
    )


# ---------------------------------------------------------------------------
# 9+: structure of the remaining models, and the six exports
# ---------------------------------------------------------------------------


def test_baseline_state_derives_the_modifier_from_persona() -> None:
    """No magic number in the model: the value comes from the placeholder."""
    for persona in ("cold", "warm", "hot"):
        state = BaselineState(persona=persona)
        assert state.persona_heat_modifier == PLACEHOLDER_PERSONA_HEAT_MODIFIER[persona]

    assert BaselineState().persona_heat_modifier == PLACEHOLDER_PERSONA_HEAT_MODIFIER["warm"]
    assert BaselineState(persona="cold", persona_heat_modifier=2.0).persona_heat_modifier == 2.0
    with pytest.raises(ValidationError):
        BaselineState(persona_heat_modifier=0.0)


def test_tension_state_defaults_and_bounds() -> None:
    tension = TensionState()

    assert tension.value == 0.0
    assert tension.last_feedback_at is None


@pytest.mark.parametrize("value", [0.0, 100.0])
def test_tension_state_accepts_both_inclusive_bounds(value: float) -> None:
    """The legal ends of the scale, not only the rejected one."""
    assert TensionState(value=value).value == value


@pytest.mark.parametrize(
    ("value", "error_type"),
    [(-1.0, "greater_than_equal"), (101.0, "less_than_equal")],
)
def test_tension_state_rejects_out_of_range(value: float, error_type: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        TensionState(value=value)

    errors = excinfo.value.errors()
    assert [error["type"] for error in errors] == [error_type]
    assert errors[0]["loc"] == ("value",)


def test_suppression_delta_is_empty_and_frozen() -> None:
    """P2.8 has no producer for this object; the docstring says so."""
    delta = SuppressionDelta()

    assert delta.extra_cortisol == 0.0
    docstring = SuppressionDelta.__doc__ or ""
    assert "P2.8" in docstring
    assert "validate_dual_axis" in docstring
    with pytest.raises(ValidationError):
        delta.extra_cortisol = 1.0  # type: ignore[misc]


def test_global_context_defaults() -> None:
    context = GlobalContext()

    assert context.version == 0
    assert isinstance(context.baseline, BaselineState)
    assert isinstance(context.tension, TensionState)
    assert isinstance(context.pulse, PulseState)
    assert context.baseline is not context.baseline.__class__()


def test_global_context_nested_defaults_are_not_shared() -> None:
    """``default_factory``, not a shared instance: mutating one must not leak."""
    first = GlobalContext()
    second = GlobalContext()

    assert first.pulse is not second.pulse
    first.pulse.dopamine = 10.0
    assert second.pulse.dopamine == 50.0


#: The six models this file owns.  Kept as a named roster (not an inline
#: literal) because the cross-lock assertion below has to compare it against the
#: auto-discovered roster, and because a roster is what a future model addition
#: would actually edit.
GLOBAL_CONTEXT_MODELS: tuple[type, ...] = (
    PulseState,
    BaselineState,
    TensionState,
    GlobalDelta,
    SuppressionDelta,
    GlobalContext,
)


@pytest.mark.parametrize(
    "model",
    GLOBAL_CONTEXT_MODELS,
    ids=lambda model: model.__name__,
)
def test_every_global_context_model_forbids_extra_fields(model: type) -> None:
    assert model.model_config.get("extra") == "forbid", f"{model.__name__} 未声明 extra=forbid"


def test_global_context_models_are_also_covered_by_the_domain_reflection() -> None:
    """The two forbid locks must agree on which models exist.

    The same invariant (``extra="forbid"`` on every domain model) is locked in
    two places that discover their models independently:

    * ``tests/domain/test_field_constraint_locks.py`` walks
      ``packages.core.domain`` with ``pkgutil.iter_modules`` and keeps the
      result in ``_MODEL_BY_NAME`` -- the only roster that is discovered rather
      than written down;
    * this file parametrizes over the hand-written ``GLOBAL_CONTEXT_MODELS``.

    Independent discovery is what makes each lock hard to fool, and it is also
    what lets them drift: a model can be added to one roster and forgotten in
    the other, and both suites stay green.  This test is the seam between them
    -- every model this file claims to own must also be visible to the
    reflection walk.

    Note on the card wording: it asked for the comparison to be against
    ``tests/domain/test_contracts.py``'s reflection.  That roster is
    ``_DOMAIN_MODULES = (contracts, state)`` -- a hand-written two-module tuple
    that does **not** contain ``global_context``, so the six models below are
    absent from it (13 entries vs 19).  Comparing against it would fail for the
    wrong reason, so the comparison targets the pkgutil roster, and the
    contracts roster is asserted to be a subset of that same roster instead.
    """
    from tests.domain.test_contracts import _DOMAIN_MODELS
    from tests.domain.test_field_constraint_locks import _MODEL_BY_NAME

    ours = {model.__name__ for model in GLOBAL_CONTEXT_MODELS}
    reflected = set(_MODEL_BY_NAME)
    contracts_roster = {model.__name__ for model in _DOMAIN_MODELS}

    assert ours, "the global_context roster is empty -- the lock would be vacuous"
    assert len(ours) == len(GLOBAL_CONTEXT_MODELS), "duplicate names in the roster"

    missing = ours - reflected
    assert not missing, (
        f"{sorted(missing)} listed in test_global_context.py but not found by the "
        f"pkgutil reflection in test_field_constraint_locks._MODEL_BY_NAME "
        f"-- the two extra=forbid locks have drifted apart"
    )

    # Reverse direction: the narrower contracts roster must also be a subset of
    # the auto-discovered one, otherwise the reflection walk has quietly
    # stopped covering a module that another lock still trusts.
    stray = contracts_roster - reflected
    assert not stray, (
        f"{sorted(stray)} listed in test_contracts.py but not found by the pkgutil "
        f"reflection -- the reflection roster has stopped covering a module that "
        f"another lock still trusts"
    )

    # A model living only in this file's roster must come from a domain module;
    # a fake injected here (base class defined in the test module) would have a
    # ``tests.*`` __module__ and is caught one line above rather than here.
    for model in GLOBAL_CONTEXT_MODELS:
        assert model.__module__ == "packages.core.domain.global_context", (
            f"{model.__name__} comes from {model.__module__}, "
            f"not from packages.core.domain.global_context"
        )
