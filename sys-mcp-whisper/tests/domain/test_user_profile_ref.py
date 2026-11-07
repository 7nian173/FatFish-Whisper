"""Contract tests for UserProfileRef (plan-p0-contracts.md section 4, G4).

UserProfileRef carries the fallback short-tag cache that the Context Router uses
when LLM routing degrades.  This file was written before the model existed: the
first run must fail at collection (ImportError) and only turns green once
``UserProfileRef`` is exported by ``packages.core.domain.contracts``.

Scope is structural only: tag vocabulary belongs to P3, so the tests lock the
shape (non-empty user_id, tuple of non-empty tags, at most 8 tags, optional
timestamp) and nothing about tag semantics.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from packages.core.domain.contracts import UserProfileRef

EIGHT_TAGS = (
    "cold_face",
    "soft_heart",
    "tsundere",
    "night_owl",
    "coffee",
    "rain",
    "cat",
    "manga",
)
NINE_TAGS = (
    "cold_face",
    "soft_heart",
    "tsundere",
    "night_owl",
    "coffee",
    "rain",
    "cat",
    "manga",
    "extra",
)


def test_user_profile_ref_defaults() -> None:
    """Only user_id is required; the cache starts empty and unfetched."""
    ref = UserProfileRef(user_id="u_8842")

    assert ref.user_id == "u_8842"
    assert ref.cached_tags == ()
    assert isinstance(ref.cached_tags, tuple)
    assert ref.tags_fetched_at is None


def test_user_profile_ref_with_tags() -> None:
    """A populated cache keeps tuple ordering and the injected timestamp."""
    fetched_at = datetime(2026, 11, 5, 12, 0, 0)
    ref = UserProfileRef(
        user_id="u_8842",
        cached_tags=("冷面", "心软"),
        tags_fetched_at=fetched_at,
    )

    assert ref.cached_tags == ("冷面", "心软")
    assert isinstance(ref.cached_tags, tuple)
    assert ref.tags_fetched_at == fetched_at


def test_user_profile_ref_tag_count_limit() -> None:
    """Nine tags are rejected while eight tags still construct.

    The positive half is deliberate: without it a model that rejects every
    non-empty tuple would also satisfy the negative assertion.
    """
    with pytest.raises(ValidationError):
        UserProfileRef(user_id="u_8842", cached_tags=NINE_TAGS)

    accepted = UserProfileRef(user_id="u_8842", cached_tags=EIGHT_TAGS)
    assert len(accepted.cached_tags) == 8
    assert accepted.cached_tags == EIGHT_TAGS


def test_user_profile_ref_rejects_empty_tag() -> None:
    """An empty string inside the tuple is rejected, as is an empty user_id."""
    with pytest.raises(ValidationError):
        UserProfileRef(user_id="u_8842", cached_tags=("冷面", ""))

    with pytest.raises(ValidationError):
        UserProfileRef(user_id="", cached_tags=())


def test_user_profile_ref_rejects_extra_field() -> None:
    """Behaviour-level guard: an unknown field must raise, not be dropped.

    Complements the static ``extra="forbid"`` sweep in ``test_contracts.py``:
    that one proves the setting is declared, this one proves it actually takes
    effect at validation time (a later ``model_config`` assignment, or an
    inheritance-chain override, could leave the declaration intact while the
    behaviour goes back to ``ignore``).
    """
    with pytest.raises(ValidationError) as excinfo:
        UserProfileRef(user_id="u_8842", bogus=1)

    errors = excinfo.value.errors()
    assert [error["type"] for error in errors] == ["extra_forbidden"]
    assert errors[0]["loc"] == ("bogus",)
