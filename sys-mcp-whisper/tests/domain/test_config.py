"""Tests for packages/core/domain/config.py (AC-C1..AC-C8).

Written before the implementation on purpose: the first run must fail with an
ImportError and only turns green once config.py exists.

Every assertion follows plan-config.md section 2 / section 3.1; the default
values are checked against the section 2.6 table exactly (one construction
source of truth), while AC-C2's command-level form only locks structure.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from packages.core.domain.config import (
    DEFAULT_ENGINE_CONFIG,
    DEFAULT_RULES,
    ConflictConfig,
    ConflictRule,
    config_fingerprint,
)


def test_default_fields_match_plan_table() -> None:
    # plan-config 3.1 case (1): every default field equals the 2.6 table.
    cfg = DEFAULT_ENGINE_CONFIG
    assert len(cfg.conflict.rules) == 2
    assert cfg.conflict.epsilon == 0.05
    assert cfg.dynamics.hormone_per_hour == {
        "dopamine": 1.0,
        "cortisol": 1.0,
        "oxytocin": 1.0,
    }
    assert cfg.dynamics.affiliation_per_hour == 0.5
    assert cfg.dynamics.daily_cap_mode == "natural_day"
    assert cfg.assembly.segment_names == {
        "main": "[主场]",
        "tension": "[拉扯]",
        "bystander": "[旁边杵着]",
    }
    assert cfg.assembly.seal_text == "绝对不要使用任何括号或星号描写动作。"
    assert cfg.assembly.lexicon_fallback == "熟悉的熟悉感漫上来。"


def test_default_rules_names_and_priorities() -> None:
    # AC-J2 / AC-J6 anchor: rule names and priorities are data, 10 and 20.
    names = [rule.name for rule in DEFAULT_RULES]
    assert names == ["dopamine_up_and_cortisol_up", "affiliation_up_and_dominance_down"]
    assert [rule.priority for rule in DEFAULT_RULES] == [10, 20]
    first = DEFAULT_RULES[0]
    assert first.conditions == (
        ("pulse.dopamine", "up"),
        ("pulse.cortisol", "up"),
    )
    second = DEFAULT_RULES[1]
    assert second.conditions == (
        ("ipc.affiliation", "up"),
        ("ipc.dominance", "down"),
    )


def test_frozen_rejects_inplace_mutation() -> None:
    # plan-config 3.1 case (4): mutation must raise FrozenInstanceError.
    with pytest.raises(FrozenInstanceError):
        DEFAULT_ENGINE_CONFIG.conflict.epsilon = 0.2  # type: ignore[misc]


def test_replace_derives_new_instance() -> None:
    # plan-config 3.1 case (3): the only mutation channel is dataclasses.replace.
    derived = replace(DEFAULT_ENGINE_CONFIG.conflict, epsilon=0.1)
    assert derived.epsilon == 0.1
    assert DEFAULT_ENGINE_CONFIG.conflict.epsilon == 0.05


def test_negative_epsilon_raises_value_error() -> None:
    # plan-config 3.1 case (5).
    with pytest.raises(ValueError):
        ConflictConfig(rules=DEFAULT_RULES, epsilon=-0.05)


def test_empty_rules_raises_value_error() -> None:
    # plan-config 3.1 case (6): empty rule table is meaningless, rejected here.
    with pytest.raises(ValueError):
        ConflictConfig(rules=(), epsilon=0.05)


def test_fingerprint_reproducible_and_valid_json() -> None:
    # AC-C6: same config, same fingerprint, and the fingerprint is valid JSON.
    fp = config_fingerprint(DEFAULT_ENGINE_CONFIG)
    assert len(fp) > 0
    assert fp == config_fingerprint(DEFAULT_ENGINE_CONFIG)
    parsed = json.loads(fp)
    assert isinstance(parsed, dict)
    assert parsed["conflict"]["epsilon"] == 0.05


def test_fingerprint_changes_when_value_changes() -> None:
    # Drift detection: a different value must yield a different fingerprint.
    other = replace(DEFAULT_ENGINE_CONFIG, conflict=replace(
        DEFAULT_ENGINE_CONFIG.conflict, epsilon=0.1
    ))
    assert config_fingerprint(other) != config_fingerprint(DEFAULT_ENGINE_CONFIG)


def test_no_load_config_channel() -> None:
    # AC-C8: no file-loading channel exists in this stage.
    from packages.core.domain import config as config_module

    assert not hasattr(config_module, "load_config")


def test_conflict_rule_is_frozen_data() -> None:
    # The rule rows themselves are immutable values (close = swap the tuple).
    rule = ConflictRule(
        name="extra_rule",
        conditions=(("pulse.oxytocin", "up"),),
        priority=30,
    )
    with pytest.raises(FrozenInstanceError):
        rule.priority = 31  # type: ignore[misc]


