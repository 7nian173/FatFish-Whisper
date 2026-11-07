"""Global context contract for P2.8 (ADR-008 sections 1.3 / 1.7 / 1.8 / 3).

P2.8 splits the single per-scope snapshot into a global layer (this module) and
the per-scope snapshot that ``state.S1Snapshot`` keeps serving. This file is the
*contract only*: it declares the shape and the bounds, and deliberately has no
behaviour beyond validation. Nothing in ``engine`` reads these models yet --
committing a ``GlobalDelta`` onto a ``GlobalContext`` is C3, and the six
placeholder numbers in ``config`` are structural placeholders, not tuned values.

Two things are load-bearing and easy to "simplify" away by accident:

* ``PulseState.delayed_impact`` keeps the dict-key validator copied from
  ``state.HormoneState``. ``extra="forbid"`` never sees dict keys and
  ``Field(ge=..., le=...)`` never reaches dict *values*, so both checks have to
  be written by hand (P0-C2 measured this).
* ``GlobalDelta`` and ``SuppressionDelta`` are ``frozen=True`` because each is an
  immutable receipt (a submitted delta, a recorded suppression cost), the same
  reason ``EmotionalEvent`` and ``RoutingResult`` are frozen. The other four
  models stay mutable because ``commit_delta`` will rebuild them in place.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import PLACEHOLDER_PERSONA_HEAT_MODIFIER

#: Field names of ``PulseState`` -- the shape of the global pulse, not the
#: vocabulary a delta may use.
#:
#: Until P2.8-C4 these were also the accepted key names of ``GlobalDelta.pulse``,
#: which made the codebase carry two vocabularies for the same three hormones.
#: The human P0 ruling retired the bare spelling: a delta now speaks the dotted
#: ``DELTA_ELIGIBLE_PATHS`` paths (``pulse.dopamine`` ...), exactly like
#: ``judge.HormoneDeltas``.  ``PULSE_KEYS`` stays because it still answers "which
#: fields does a ``PulseState`` have".
PULSE_KEYS: tuple[str, ...] = ("dopamine", "cortisol", "oxytocin", "delayed_impact")

_IMPACT_KEYS: frozenset[str] = frozenset({"dopamine", "cortisol", "oxytocin"})


#: The complete set of dotted paths a delta may address (ADR-008 section 1.8).
#:
#: Deliberately a literal and NOT derived from any model.  Before P2.8 the
#: whitelist was computed as ``S1Snapshot.valid_paths()`` -- 16 paths including
#: ``timestamp`` / ``scope_id`` / ``traits`` / ``emotions``, values that cannot
#: be summed.  Narrowing it to additive numeric paths is a contract decision,
#: so it belongs in a constant a reviewer can read, not in a side effect of
#: which fields happen to live on which model.
#:
#: The five members:
#:
#: * ``pulse.dopamine`` / ``pulse.cortisol`` / ``pulse.oxytocin`` -- 0..100
#:   floats that converge on a 50 baseline, so an increment is meaningful;
#: * ``ipc.affiliation`` / ``ipc.dominance`` -- -100..100 per-user floats.
#:
#: ``pulse.delayed_impact`` is NOT a member even though ``PulseState`` carries
#: it: that field is the container for deferred increments ("in transit, not
#: yet effective"), not a scalar a stimulus can be added to.
DELTA_ELIGIBLE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "pulse.dopamine",
        "pulse.cortisol",
        "pulse.oxytocin",
        "ipc.affiliation",
        "ipc.dominance",
    }
)

#: The ``pulse.*`` slice of the whitelist -- carried by ``GlobalDelta.pulse``.
#:
#: Derived, never enumerated: the whitelist has exactly one physical home and
#: these two sets only split it by namespace.  A path added to the whitelist
#: lands in one carrier automatically, which is what keeps "constructible" and
#: "applicable" the same set.
_PULSE_DELTA_PATHS: Final[frozenset[str]] = frozenset(
    path for path in DELTA_ELIGIBLE_PATHS if path.startswith("pulse.")
)

#: The ``ipc.*`` slice of the whitelist -- carried by ``GlobalDelta.ipc``.
_IPC_DELTA_PATHS: Final[frozenset[str]] = frozenset(
    path for path in DELTA_ELIGIBLE_PATHS if path.startswith("ipc.")
)


class PulseState(BaseModel):
    """Fast-changing hormone layer (the former home of ``S1Snapshot.hormones``)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    dopamine: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="多巴胺：全局共享（非 per-user），基线 50，向 50 线性回归",
    )
    cortisol: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="皮质醇：全局共享，基线 50，向 50 线性回归",
    )
    oxytocin: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="催产素：全局共享，基线 50，向 50 线性回归",
    )
    delayed_impact: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "HPA 轴延迟：在途未生效增量，到期后并入对应激素再归零"
            "（合并逻辑属 P1-03，domain 层只存结构）；"
            "键限 dopamine / cortisol / oxytocin，值 0..100"
        ),
    )

    @field_validator("delayed_impact")
    @classmethod
    def _delayed_impact_keys_must_be_hormones(cls, v: dict[str, float]) -> dict[str, float]:
        # Copied verbatim in intent from state.HormoneState (see the module
        # docstring): extra="forbid" cannot see dict keys and Field(ge/le)
        # cannot reach dict values, so both checks live here, key by key.
        for key in v:
            if key not in _IMPACT_KEYS:
                raise ValueError(f"unknown delayed_impact key: {key}")
            if not (0.0 <= v[key] <= 100.0):
                raise ValueError(f"delayed_impact[{key}]={v[key]} out of range [0, 100]")
        return v


class BaselineState(BaseModel):
    """Extremely slow drift layer: the persona baseline."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    persona: Literal["cold", "warm", "hot"] = Field(
        default="warm",
        description="人设档位; 取值必须与 PLACEHOLDER_PERSONA_HEAT_MODIFIER 的键集合相等",
    )
    persona_heat_modifier: float = Field(
        gt=0.0,
        description=(
            "人设热度系数, 默认由 persona 从 PLACEHOLDER_PERSONA_HEAT_MODIFIER 推导; "
            "模型内不写死任何数值"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_modifier_from_persona(cls, data: object) -> object:
        # Derived rather than defaulted: a literal ``default=1.0`` in the model
        # would be a magic number that only *claims* to equal the placeholder,
        # and the two would drift the first time P4 re-tunes the table.
        if isinstance(data, dict) and "persona_heat_modifier" not in data:
            persona = data.get("persona", "warm")
            merged = dict(data)
            merged["persona_heat_modifier"] = PLACEHOLDER_PERSONA_HEAT_MODIFIER[persona]
            return merged
        return data


class TensionState(BaseModel):
    """Medium-speed accumulation layer: tension."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    value: float = Field(default=0.0, ge=0.0, le=100.0, description="张力值, 0 到 100")
    last_feedback_at: datetime | None = Field(
        default=None,
        description="最近一次收到反馈的时刻, 由调用方注入 (domain 不读时钟)",
    )


class GlobalDelta(BaseModel):
    """A pending global increment, submitted as one immutable receipt.

    Keys are dotted ``DELTA_ELIGIBLE_PATHS`` paths, split across two carriers by
    namespace: ``pulse`` takes ``pulse.dopamine`` / ``pulse.cortisol`` /
    ``pulse.oxytocin``, ``ipc`` takes ``ipc.affiliation`` / ``ipc.dominance``.
    The split exists so a delta names the same fields a ``PulseState`` or an
    ``IPCState`` has -- there is no third vocabulary -- while keeping the two
    state objects separate.  Every whitelist member lands in exactly one
    carrier, so "constructible" and "applicable" are the same set.

    ``pulse.delayed_impact`` is deliberately NOT eligible: ``delayed_impact`` is
    a ``dict[str, float]`` of pending per-hormone increments, and
    ``pulse.delayed_impact + 1.0`` raises TypeError, so it cannot carry a scalar
    delta (measured, see the C4 report).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pulse: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "点分脉冲路径到增量的映射; 键限 DELTA_ELIGIBLE_PATHS 的 pulse.* 成员"
        ),
    )
    ipc: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "点分 IPC 路径到增量的映射; 键限 DELTA_ELIGIBLE_PATHS 的 ipc.* 成员"
        ),
    )
    tension: float = Field(default=0.0, description="张力增量")

    @field_validator("pulse")
    @classmethod
    def _pulse_keys_must_be_delta_eligible(
        cls, v: dict[str, float]
    ) -> dict[str, float]:
        for key in v:
            if key not in _PULSE_DELTA_PATHS:
                raise ValueError(f"unknown pulse key: {key}")
        return v

    @field_validator("ipc")
    @classmethod
    def _ipc_keys_must_be_delta_eligible(cls, v: dict[str, float]) -> dict[str, float]:
        for key in v:
            if key not in _IPC_DELTA_PATHS:
                raise ValueError(f"unknown ipc key: {key}")
        return v


class SuppressionDelta(BaseModel):
    """Suppression cost increment.

    P2.8 阶段无代码生成此对象; 它预留给 P3 的 ``validate_dual_axis`` 在拦截 OOC 时产出。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    extra_cortisol: float = Field(default=0.0, description="压抑额外抬升的皮质醇增量")


class GlobalContext(BaseModel):
    """The one global context, shared across every ``scope_id``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    version: int = Field(default=0, ge=0, description="提交版本号, 每次 commit_delta 递增")
    baseline: BaselineState = Field(default_factory=BaselineState)
    tension: TensionState = Field(default_factory=TensionState)
    pulse: PulseState = Field(default_factory=PulseState)
    # Reserved: C3 may add resident_since / last_active_at. Not in this card.


__all__ = [
    "DELTA_ELIGIBLE_PATHS",
    "PULSE_KEYS",
    "BaselineState",
    "GlobalContext",
    "GlobalDelta",
    "PulseState",
    "SuppressionDelta",
    "TensionState",
]


# The four PulseState descriptions are copied verbatim from state.HormoneState
# (contract 3.2) and the SuppressionDelta docstring is verbatim from the card's
# human ruling. Both keep the original full-width CJK punctuation, which RUF001
# (strings) and RUF002 (docstrings) flag as ambiguous -- same posture as
# state.py and contracts.py. Every other string in this file uses ASCII
# punctuation on purpose, so the suppression stays as narrow as possible.
# ruff: noqa: RUF001
# Scope is RUF001 only. Four Field descriptions are copied verbatim from
# state.py / contract 3.2 and therefore keep their full-width CJK punctuation;
# rewriting them to ASCII would break the "verbatim" ruling. RUF002 (docstrings)
# is NOT exempt: this file's docstrings use half-width punctuation on purpose.
