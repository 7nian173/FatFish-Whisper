"""Anti-corruption guards for data entering the core (task-FFW-P0-C7).

The seven guards below transcribe `P0-防腐层要求.md` section 1 one-for-one.  Each
one is a pure function: input is a plain value, output is a plain value or an
exception.  No IO, no clock, no import of the frozen legacy packages -- the real
adapter wiring is P3-02's job, this card only lands the criteria.

Three ground rules from that document, quoted because they justify the shapes:

* 只拦不改 -- the legacy library stays byte-identical; every check happens on
  the new side of the boundary.
* 强制失败不静默 -- when a key name, a required value, or a source-trust fact is
  wrong, raise.  Never substitute a default.
* 错误消息可断言 -- the message format is fixed, so tests can lock it and a
  wording change turns red instead of drifting quietly.

Exception types are limited to ``ValueError`` / ``KeyError`` / ``RuntimeError``:
the document forbids custom exception classes, and the domain whitelist would
not accept one anyway.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "REQUIRED_PAYLOAD_KEYS",
    "assemble_payload",
    "guard_xff_chain",
    "normalize_message_id",
    "require_event_key",
    "require_user_id",
    "sanitize_error_body",
    "validate_core_extra_hosts",
]

#: The six keys a payload handed to the core must carry.
#:
#: Derived from the duplicate-branch skeleton at the frozen library's
#: ``main.py:696-711`` (``status`` / ``duplicate`` / ``applied`` / ``actions`` /
#: ``inner_os`` / ``debug``).  Defect 0039-D7 is that this skeleton never carries
#: ``recall_hint`` while the consumer indexes it directly, so ``recall_hint``
#: takes a slot in the required set.  ``debug`` leaves the set in exchange: it is
#: a diagnostic bag that the consumer always reads through ``.get("debug") or {}``
#: and never by subscript, so requiring it would enforce a shape nothing depends
#: on.  The five keys common to both skeletons plus ``recall_hint`` give six.
#:
#: The order below is the legacy skeleton's own semantic order and is
#: deliberately *not* alphabetical.  Message determinism therefore comes from the
#: ``sorted()`` at the raise site, not from this constant.  Had the constant been
#: pre-sorted, deleting that call would leave every message identical and no test
#: could notice -- which is exactly what the first non-vacuity run showed.
REQUIRED_PAYLOAD_KEYS: tuple[str, ...] = (
    "status",
    "duplicate",
    "applied",
    "actions",
    "inner_os",
    "recall_hint",
)

#: Message for defect 0039-D1.  ``DEFAULT_USER_ID = "10001"`` is *not* imported,
#: not re-declared, and not referenced anywhere in this module.
_MSG_USER_ID = "FFW-0039-D1: user_id is required and must be non-empty"

#: Message for defect 0039-D5 (empty value).  A *missing* key is a different
#: path: it does not raise, it sets ``event_key_fallback`` on the payload.
_MSG_EVENT_KEY = "FFW-0039-D5: event_key is empty, refusing silent fallback"

#: Message prefix for defect 0039-D7; the missing-key list is appended.
_MSG_PAYLOAD_PREFIX = "FFW-0039-D7: payload must contain all six keys, missing: "

#: Message for defect 0038-D1.  ``None`` is a legal value (absent), ``""`` is
#: not: the defect is exactly the legacy habit of flattening both to ``""``.
_MSG_MESSAGE_ID = "FFW-0038-D1: message_id must not be empty string; use None for absent"

#: Message for defect 0041-D3.
_MSG_XFF = "FFW-0041-D3: XFF chain present but TRUSTED_PROXIES not explicitly configured"

#: Message prefix for defect 0042-D7; the offending entry is quoted inside.
_MSG_EXTRA_HOSTS_PREFIX = "FFW-0042-D7: CORE_EXTRA_HOSTS entry "

#: The only body text permitted to leave this layer (defect 0039-D6).  Used both
#: as the shape template and as the replacement text when a caller hands in a
#: body that could leak.
_MSG_ERROR_BODY = "FFW-0039-D6: internal routing failure"

#: Shape a return body must have: fixed code plus a lowercase narrative.
_ERROR_BODY_PATTERN = re.compile(r"^FFW-\d{4}-D\d+: [a-z]+(?: [a-z]+)*$")

#: A whitelist entry is ``host`` or ``host:port``: no scheme, no path, no space.
#: Port is digits only -- ``host:port`` is a malformed entry, not a hostname.
_HOST_ENTRY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?::\d{1,5})?$"
)

#: Fragments that must never survive into a body returned to the host.
_LEAKY_FRAGMENTS: tuple[str, ...] = ("Traceback", "Error:", "exception")


def require_user_id(user_id: str | None) -> str:
    """Return ``user_id`` unchanged, or raise if it is missing or blank.

    Blank means empty after ``str.strip()``, which also catches the full-width
    ideographic space a CJK client can send.  The *returned* value is never
    stripped: trimming would silently rewrite an identifier the legacy store
    may treat as distinct.
    """
    if user_id is None or not user_id.strip():
        raise ValueError(_MSG_USER_ID)
    return user_id


def require_event_key(event_key: str | None) -> str:
    """Return ``event_key`` unchanged, or raise when its value is empty.

    Only the empty and ``None`` cases raise.  A key that is merely absent is a
    separate path handled by :func:`assemble_payload`, which marks it instead of
    raising.
    """
    if not event_key:
        raise ValueError(_MSG_EVENT_KEY)
    return event_key


def assemble_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return a payload carrying all six required keys.

    A missing required key raises with the keys sorted, so the message text does
    not depend on set iteration order.  ``recall_hint`` present with a ``None``
    value is legal -- that is the whole point: the key exists, only its data is
    absent.

    When ``event_key`` is absent from the input the returned payload carries
    ``event_key_fallback=True``.  The frozen library degrades this case silently;
    this layer keeps the degradation visible to the caller instead of hiding it.
    """
    missing = sorted(key for key in REQUIRED_PAYLOAD_KEYS if key not in data)
    if missing:
        raise KeyError(_MSG_PAYLOAD_PREFIX + str(missing))
    payload: dict[str, Any] = dict(data)
    if not payload.get("event_key"):
        payload["event_key_fallback"] = True
    return payload


def normalize_message_id(message_id: str | None) -> str | None:
    """Return ``None`` for absent, an id for present, and raise for ``""``.

    The signature itself carries the distinction: ``-> str | None`` says the
    absence is representable, so an empty string is never a stand-in for it.
    """
    if message_id is None:
        return None
    if message_id == "":
        raise ValueError(_MSG_MESSAGE_ID)
    return message_id


def guard_xff_chain(
    xff_header: str | None,
    trusted_proxies: tuple[str, ...] | None,
) -> dict[str, Any]:
    """Report whether an XFF chain is present, refusing an unconfigured trust list.

    A chain exists when the header contributes at least one non-blank hop.  Every
    hop after the first can be forged by the client, so an unconfigured
    ``TRUSTED_PROXIES`` makes the chain unusable as a source-of-truth: raise
    rather than quietly falling back to the peer address.
    """
    hops = [hop.strip() for hop in (xff_header or "").split(",")]
    hops = [hop for hop in hops if hop]
    if not hops:
        return {"xff_present": False, "hops": []}
    if not trusted_proxies:
        raise RuntimeError(_MSG_XFF)
    return {"xff_present": True, "hops": hops}


def validate_core_extra_hosts(entries: list[str]) -> list[str]:
    """Return the accepted entries in input order; raise on the first bad one.

    Validation is per entry and does not rewrite anything: ``len(accepted)``
    always equals ``len(entries)``, because silently dropping a malformed entry
    would shrink an allowlist without anyone noticing.
    """
    accepted: list[str] = []
    for entry in entries:
        if not _HOST_ENTRY_PATTERN.match(entry):
            raise ValueError(_MSG_EXTRA_HOSTS_PREFIX + f"'{entry}' fails validation")
        accepted.append(entry)
    return accepted


def sanitize_error_body(body: str) -> str:
    """Return ``body`` when it conforms, otherwise raise the standard text.

    Raising rather than substituting is deliberate: rewriting a response body is
    the gateway's job (``P0-防腐层要求.md`` section 2 draws that line), and a
    silent replacement here would make a leak look like a normal failure.  The
    raised message is itself conforming, so even the failure path cannot carry
    stack text outward.
    """
    if not _ERROR_BODY_PATTERN.match(body):
        raise ValueError(_MSG_ERROR_BODY)
    for fragment in _LEAKY_FRAGMENTS:
        if fragment in body:
            raise ValueError(_MSG_ERROR_BODY)
    return body
