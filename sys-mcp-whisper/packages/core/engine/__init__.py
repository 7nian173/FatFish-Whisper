"""Engine layer: pure dynamics, conflict judgement, routing and prompt assembly."""

from __future__ import annotations

from .arbiter import DictionaryArbiter
from .assembler import MOCK_LEXICON, assemble_messages, assemble_prompt
from .commit import commit_delta
from .delay import calculate_typing_delay
from .dynamics import (
    DayAccumulator,
    DecayRates,
    StimulusRecord,
    apply_limit_cap,
    apply_time_decay,
    update_hormones,
)
from .heat import (
    apply_persona_ceiling,
    calculate_dynamic_heat,
    calculate_effective_heat,
)
from .judge import ConflictVerdict, HormoneDeltas, is_conflict
from .reconciliation import (
    AsyncReconciliationQueue,
    PendingDelta,
    apply_feedback,
)
from .router import (
    COLD_NARRATION,
    VALID_TIERS,
    calculate_heat,
    evaluate_compute_tier,
    merge_cross_domain_state,
    sanitize_tier_state,
    settle_before_tier_upgrade,
    should_call_track_b,
)
from .routing import (
    SCENARIO_TAGS,
    MessageEnvelope,
    Segment,
    parse_speaker,
    route_track_a,
    route_track_b,
)
from .stimulus import to_delta

__all__ = [
    "COLD_NARRATION",
    "MOCK_LEXICON",
    "SCENARIO_TAGS",
    "VALID_TIERS",
    "AsyncReconciliationQueue",
    "ConflictVerdict",
    "DayAccumulator",
    "DecayRates",
    "DictionaryArbiter",
    "HormoneDeltas",
    "MessageEnvelope",
    "PendingDelta",
    "Segment",
    "StimulusRecord",
    "apply_feedback",
    "apply_limit_cap",
    "apply_persona_ceiling",
    "apply_time_decay",
    "assemble_messages",
    "assemble_prompt",
    "calculate_dynamic_heat",
    "calculate_effective_heat",
    "calculate_heat",
    "calculate_typing_delay",
    "commit_delta",
    "evaluate_compute_tier",
    "is_conflict",
    "merge_cross_domain_state",
    "parse_speaker",
    "route_track_a",
    "route_track_b",
    "sanitize_tier_state",
    "settle_before_tier_upgrade",
    "should_call_track_b",
    "to_delta",
    "update_hormones",
]
