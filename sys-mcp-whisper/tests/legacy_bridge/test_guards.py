"""Locks for the P0 anti-corruption guards (task-FFW-P0-C7).

Each of the seven guards from ``P0-防腐层要求.md`` gets at least three cells:
a legal input that must pass, an illegal input that must be rejected, and a
boundary input that separates the three states (``None`` / empty string /
whitespace-only).  Rejection cells assert the message verbatim, because the
message *is* the contract: the wording is what P3-02's adapter and the gateway
regex are written against.

``_ALL_GUARD_MESSAGES`` collects the seven canonical texts so the shape test can
guard against wording drift even if someone forgets to update a per-cell assert.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from packages.legacy_bridge import guards
from packages.legacy_bridge.guards import (
    REQUIRED_PAYLOAD_KEYS,
    assemble_payload,
    guard_xff_chain,
    normalize_message_id,
    require_event_key,
    require_user_id,
    sanitize_error_body,
    validate_core_extra_hosts,
)

# --- the seven canonical messages (the contract) -----------------------------

MSG_USER_ID = "FFW-0039-D1: user_id is required and must be non-empty"
MSG_EVENT_KEY = "FFW-0039-D5: event_key is empty, refusing silent fallback"
MSG_PAYLOAD_PREFIX = "FFW-0039-D7: payload must contain all six keys, missing: "
MSG_MESSAGE_ID = "FFW-0038-D1: message_id must not be empty string; use None for absent"
MSG_XFF = "FFW-0041-D3: XFF chain present but TRUSTED_PROXIES not explicitly configured"
MSG_EXTRA_HOSTS_PREFIX = "FFW-0042-D7: CORE_EXTRA_HOSTS entry "
MSG_ERROR_BODY = "FFW-0039-D6: internal routing failure"

#: Every guard's canonical message.  Seven entries: one per required code.
#: The two entries built by ``str.format`` are still static text -- only the
#: interpolated tail varies, so the wording lock below still bites.
_ALL_GUARD_MESSAGES: tuple[str, ...] = (
    MSG_USER_ID,
    MSG_EVENT_KEY,
    MSG_PAYLOAD_PREFIX + "['recall_hint']",
    MSG_MESSAGE_ID,
    MSG_XFF,
    MSG_EXTRA_HOSTS_PREFIX + "'bad entry' fails validation",
    MSG_ERROR_BODY,
)

#: Substrings that must never reach the user (P0-防腐层要求.md section 1.7).
_FORBIDDEN_BODY_FRAGMENTS = ("Traceback", "Error:", "exception")


def _message_of(exc: BaseException) -> str:
    """Return the contract text of ``exc`` without Python's repr decoration.

    ``str(KeyError("..."))`` is ``repr()`` of the argument, so it comes back
    wrapped in single quotes and would never equal the contract text.  ``args[0]``
    is the argument as raised; for ``ValueError`` and ``RuntimeError`` it equals
    ``str()``, so one accessor serves all three exception types.
    """
    assert exc.args, exc
    return str(exc.args[0])


def _rejects(exc_type: type[BaseException], message: str, func, *args) -> None:
    """Call ``func`` and require exactly ``message`` from ``exc_type``.

    ``==`` rather than a substring test: a longer message that still *contains*
    the contract text would pass ``match=`` and slip through review.
    """
    with pytest.raises(exc_type) as excinfo:
        func(*args)
    assert _message_of(excinfo.value) == message


# --- 1. require_user_id (FFW-0039-D1) ---------------------------------------


def test_require_user_id_returns_the_value_verbatim() -> None:
    assert require_user_id("u1") == "u1"


def test_require_user_id_does_not_strip() -> None:
    # Stripping would silently change ID semantics: " u1 " and "u1" may be
    # distinct accounts in the legacy store, so the value must survive untouched.
    assert require_user_id(" u1 ") == " u1 "


@pytest.mark.parametrize(
    "bad",
    [None, "", "   ", " \t\n ", "\u3000\u3000"],
    ids=["none", "empty", "spaces", "mixed-whitespace", "ideographic-space"],
)
def test_require_user_id_rejects_missing_and_blank(bad: object) -> None:
    _rejects(ValueError, MSG_USER_ID, require_user_id, bad)


def test_require_user_id_never_falls_back_to_the_legacy_default() -> None:
    # The whole point of D1: no DEFAULT_USER_ID constant may be substituted.
    with pytest.raises(ValueError) as excinfo:
        require_user_id(None)
    assert "10001" not in str(excinfo.value)
    assert not hasattr(guards, "DEFAULT_USER_ID")


# --- 2. require_event_key (FFW-0039-D5) -------------------------------------


def test_require_event_key_returns_the_value_verbatim() -> None:
    assert require_event_key("ek-1") == "ek-1"


@pytest.mark.parametrize("bad", [None, ""], ids=["none", "empty"])
def test_require_event_key_rejects_empty_value(bad: object) -> None:
    _rejects(ValueError, MSG_EVENT_KEY, require_event_key, bad)


def test_require_event_key_matches_with_re_escape() -> None:
    # Also exercise the ``match=`` form the card asks for, on the same text.
    with pytest.raises(ValueError, match=re.escape(MSG_EVENT_KEY)):
        require_event_key("")


def test_require_event_key_keeps_whitespace_only_values() -> None:
    # The requirement names only empty-string and None.  A whitespace-only key is
    # junk, but rejecting it would be this card *adding* a rule instead of
    # enforcing a documented one, so it passes through unchanged.
    assert require_event_key("  ") == "  "


# --- 3. assemble_payload (FFW-0039-D7) --------------------------------------


def _six_key_payload() -> dict:
    return {
        "status": "duplicate",
        "duplicate": True,
        "applied": False,
        "actions": (),
        "inner_os": "",
        "recall_hint": None,
    }


def test_required_payload_keys_is_exactly_six_and_includes_recall_hint() -> None:
    assert isinstance(REQUIRED_PAYLOAD_KEYS, tuple)
    assert len(REQUIRED_PAYLOAD_KEYS) == 6
    assert "recall_hint" in REQUIRED_PAYLOAD_KEYS


def test_assemble_payload_accepts_a_complete_payload() -> None:
    payload = assemble_payload(_six_key_payload())
    assert set(REQUIRED_PAYLOAD_KEYS) <= set(payload)
    assert payload["status"] == "duplicate"
    assert payload["recall_hint"] is None


def test_assemble_payload_requires_recall_hint_even_when_the_value_is_none() -> None:
    # Key present with a None value is legal; a missing key is not.
    assert assemble_payload(_six_key_payload())["recall_hint"] is None


def test_assemble_payload_rejects_a_payload_missing_recall_hint() -> None:
    incomplete = _six_key_payload()
    del incomplete["recall_hint"]
    _rejects(
        KeyError,
        MSG_PAYLOAD_PREFIX + "['recall_hint']",
        assemble_payload,
        incomplete,
    )


def test_assemble_payload_lists_missing_keys_in_deterministic_order() -> None:
    incomplete = _six_key_payload()
    del incomplete["recall_hint"]
    del incomplete["actions"]
    # sorted(), not raw set order: otherwise this assertion flaps between runs.
    _rejects(
        KeyError,
        MSG_PAYLOAD_PREFIX + "['actions', 'recall_hint']",
        assemble_payload,
        incomplete,
    )


def test_missing_key_message_is_sorted_not_declaration_ordered() -> None:
    # ``status`` precedes ``applied`` in the constant's declaration order, so a
    # run that merely walked the tuple would emit ``['status', 'applied']``.
    # Only an explicit sort produces the message below -- this is the cell that
    # catches a dropped ``sorted()`` call.
    incomplete = _six_key_payload()
    del incomplete["status"]
    del incomplete["applied"]
    _rejects(
        KeyError,
        MSG_PAYLOAD_PREFIX + "['applied', 'status']",
        assemble_payload,
        incomplete,
    )


def test_required_payload_keys_declaration_order_is_not_alphabetical() -> None:
    # Guards the guard: were the constant itself pre-sorted, the sorted() at the
    # raise site would be a no-op and the test above could no longer tell whether
    # it still exists.  Semantic order is a load-bearing property, not styling.
    assert list(REQUIRED_PAYLOAD_KEYS) != sorted(REQUIRED_PAYLOAD_KEYS)


def test_assemble_payload_returns_a_copy_and_leaves_the_input_alone() -> None:
    source = _six_key_payload()
    built = assemble_payload(source)
    assert built is not source
    built["status"] = "ok"
    assert source["status"] == "duplicate"


def test_assemble_payload_marks_a_missing_event_key_instead_of_raising() -> None:
    # P0-防腐层要求.md 1.2: a missing event_key is *not* an exception path, but it
    # must be visibly marked rather than silently degraded.
    payload = assemble_payload(_six_key_payload())
    assert payload["event_key_fallback"] is True


def test_assemble_payload_does_not_mark_a_present_event_key() -> None:
    source = _six_key_payload()
    source["event_key"] = "ek-1"
    payload = assemble_payload(source)
    assert "event_key_fallback" not in payload
    assert payload["event_key"] == "ek-1"


# --- 4. normalize_message_id (FFW-0038-D1) ----------------------------------


def test_normalize_message_id_maps_none_to_none() -> None:
    # "absent" keeps its own representation; it must not be flattened to "".
    assert normalize_message_id(None) is None


def test_normalize_message_id_returns_a_real_id() -> None:
    assert normalize_message_id("m-1") == "m-1"


def test_normalize_message_id_rejects_the_empty_string() -> None:
    _rejects(ValueError, MSG_MESSAGE_ID, normalize_message_id, "")


def test_normalize_message_id_keeps_whitespace_only_values() -> None:
    # Same reasoning as require_event_key: only "" is named by the requirement.
    assert normalize_message_id("  ") == "  "


# --- 5. guard_xff_chain (FFW-0041-D3) ---------------------------------------


def test_guard_xff_chain_without_a_chain_is_not_an_error() -> None:
    assert guard_xff_chain(None, None) == {"xff_present": False, "hops": []}
    assert guard_xff_chain("", ()) == {"xff_present": False, "hops": []}


def test_guard_xff_chain_rejects_an_unconfigured_proxy_list() -> None:
    _rejects(RuntimeError, MSG_XFF, guard_xff_chain, "203.0.113.7", None)


def test_guard_xff_chain_treats_an_empty_proxy_tuple_as_unconfigured() -> None:
    _rejects(RuntimeError, MSG_XFF, guard_xff_chain, "203.0.113.7", ())


def test_guard_xff_chain_splits_and_strips_hops() -> None:
    result = guard_xff_chain("203.0.113.7, 198.51.100.4", ("10.0.0.1",))
    assert result == {"xff_present": True, "hops": ["203.0.113.7", "198.51.100.4"]}


def test_guard_xff_chain_drops_blank_hops() -> None:
    result = guard_xff_chain(" 203.0.113.7 , , 198.51.100.4 ", ("10.0.0.1",))
    assert result == {"xff_present": True, "hops": ["203.0.113.7", "198.51.100.4"]}


def test_guard_xff_chain_treats_separators_only_as_no_chain() -> None:
    # A header of nothing but commas carries no hop, so there is no chain to
    # distrust and no reason to demand TRUSTED_PROXIES.
    assert guard_xff_chain(" , ", None) == {"xff_present": False, "hops": []}


# --- 6. validate_core_extra_hosts (FFW-0042-D7) -----------------------------


def test_validate_core_extra_hosts_accepts_an_empty_list() -> None:
    assert validate_core_extra_hosts([]) == []


def test_validate_core_extra_hosts_accepts_host_and_host_port() -> None:
    entries = ["good.host:8080", "other.host", "10.0.0.1:443", "localhost"]
    assert validate_core_extra_hosts(entries) == entries


def test_validate_core_extra_hosts_does_not_drop_entries() -> None:
    entries = ["a.host", "b.host:1", "c.host"]
    assert len(validate_core_extra_hosts(entries)) == len(entries)


def test_validate_core_extra_hosts_rejects_a_bad_entry_and_names_it() -> None:
    _rejects(
        ValueError,
        MSG_EXTRA_HOSTS_PREFIX + "'bad entry' fails validation",
        validate_core_extra_hosts,
        ["good.host:1", "bad entry"],
    )


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "http://good.host", "good.host/path", "good.host:port", " good.host", "a b.host"],
    ids=["empty", "blank", "scheme", "path", "bad-port", "leading-space", "inner-space"],
)
def test_validate_core_extra_hosts_rejects_each_malformed_shape(bad: str) -> None:
    _rejects(
        ValueError,
        MSG_EXTRA_HOSTS_PREFIX + f"'{bad}' fails validation",
        validate_core_extra_hosts,
        [bad],
    )


def test_validate_core_extra_hosts_returns_a_copy() -> None:
    entries = ["a.host"]
    accepted = validate_core_extra_hosts(entries)
    assert accepted is not entries


# --- 7. sanitize_error_body (FFW-0039-D6) -----------------------------------


def test_sanitize_error_body_returns_a_conforming_body_unchanged() -> None:
    assert sanitize_error_body(MSG_ERROR_BODY) == MSG_ERROR_BODY
    conforming = "FFW-0041-D3: xff chain present but trusted proxies not configured"
    assert sanitize_error_body(conforming) == conforming


@pytest.mark.parametrize(
    "leaky",
    [
        "Traceback (most recent call last):\n  File \"x.py\", line 1",
        "Error: boom",
        MSG_ERROR_BODY + " exception",
        "FFW-0039-D6: Internal Routing Failure",
        "FFW-39-D6: short code",
        "FFW-0039-D6:",
        "judge failed: KeyError('x')",
    ],
    ids=[
        "traceback",
        "error-prefix",
        "exception-word",
        "uppercase",
        "short-code",
        "empty-tail",
        "free",
    ],
)
def test_sanitize_error_body_rejects_a_leaky_body(leaky: str) -> None:
    _rejects(ValueError, MSG_ERROR_BODY, sanitize_error_body, leaky)


def test_sanitize_error_body_output_carries_no_stack_text() -> None:
    body = sanitize_error_body(MSG_ERROR_BODY)
    for fragment in _FORBIDDEN_BODY_FRAGMENTS:
        assert fragment not in body


# --- message-shape lock (P0-防腐层要求.md section 1.7) ----------------------

_CODE_PREFIX = re.compile(r"^FFW-\d{4}-D\d+: ")
_NARRATIVE_SHAPE = re.compile(r"^[a-z]+(?: [a-z]+)*$")


def test_every_guard_message_matches_the_error_code_shape() -> None:
    assert len(_ALL_GUARD_MESSAGES) == 7
    for message in _ALL_GUARD_MESSAGES:
        # The code prefix is fixed: an `FFW-` code, four digits, `-D`, digits.
        assert _CODE_PREFIX.match(message), message
        tail = message.split(": ", 1)[1]
        # The requirement's literal `^FFW-\d{4}-D\d+: [a-z ]+$` holds for the
        # D6 body and for no other message: the same document mandates
        # identifiers (`user_id`, `event_key`, `CORE_EXTRA_HOSTS`), an acronym
        # pair (`XFF`, `TRUSTED_PROXIES`), and two interpolated values (the
        # sorted missing-key list, the offending host entry).  What the rule is
        # actually protecting is "lowercase English narrative", so identifiers
        # and values are folded to spaces and the surviving prose is checked.
        #
        # Two honest limits, so this lock is not credited with more than it does:
        #   - Reworded English still PASSES here (it folds to valid prose).  That
        #     is deliberate: this is a shape lock, not a wording lock.  Wording is
        #     pinned by the exact-string assertions elsewhere in this file.
        #   - Stack text is NOT caught here: "Traceback" folds to "raceback",
        #     which is valid prose.  Blocking it is the job of
        #     ``test_no_guard_message_carries_stack_text`` above, which scans the
        #     unfiltered text.
        folded = re.sub(r"[^a-z]+", " ", tail).strip()
        assert _NARRATIVE_SHAPE.match(folded), (message, folded)


def test_no_guard_message_carries_stack_text() -> None:
    for message in _ALL_GUARD_MESSAGES:
        for fragment in _FORBIDDEN_BODY_FRAGMENTS:
            assert fragment not in message, (message, fragment)


# --- module-level hygiene (no IO, no legacy imports) ------------------------

_GUARDS_PATH = Path(guards.__file__)
_BANNED_MODULES = frozenset(
    {
        "sqlite3",
        "os",
        "sys",
        "pathlib",
        "socket",
        "requests",
        "httpx",
        "subprocess",
        "io",
        "shutil",
        "logging",
    }
)
_BANNED_CALLS = frozenset({"open", "print", "input", "exec", "eval", "compile", "__import__"})
_BANNED_ATTRIBUTE_CALLS = frozenset({"now", "utcnow", "today", "fromtimestamp"})


def test_guards_module_imports_only_pure_modules() -> None:
    tree = ast.parse(_GUARDS_PATH.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                seen.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module is not None
            seen.add(node.module.split(".")[0])
    assert seen <= {"__future__", "re", "typing"}, seen
    assert not {name for name in seen if name.startswith("old")}, seen


def test_guards_module_performs_no_io_and_reads_no_clock() -> None:
    tree = ast.parse(_GUARDS_PATH.read_text(encoding="utf-8"))
    flagged: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
                flagged.append(f"call:{node.func.id}")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in _BANNED_ATTRIBUTE_CALLS:
                flagged.append(f"attr:{node.func.attr}")
    assert flagged == []


def test_legacy_bridge_reexports_the_seven_guards() -> None:
    expected = {
        "REQUIRED_PAYLOAD_KEYS",
        "assemble_payload",
        "guard_xff_chain",
        "normalize_message_id",
        "require_event_key",
        "require_user_id",
        "sanitize_error_body",
        "validate_core_extra_hosts",
    }
    assert expected <= set(guards.__all__)
    assert list(guards.__all__) == sorted(guards.__all__)
