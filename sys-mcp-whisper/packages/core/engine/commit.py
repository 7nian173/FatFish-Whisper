"""The global commit step (ADR-009 section S2): tension and version only.

``commit_delta`` applies a :class:`GlobalDelta` to a :class:`GlobalContext` and
returns the next context.  It commits exactly two things:

* ``tension`` -- clamped into ``0..100`` before the model round trip;
* ``version`` -- incremented by one, which is what makes the commit observable.

Three design points are load-bearing.

**Why the hormone and IPC carriers are rejected rather than ignored.**  A
``pulse.*`` or ``ipc.*`` key is a perfectly valid :class:`GlobalDelta` (both
carriers are part of the contract), so this function *will* be handed one
sooner or later.  Ignoring it would return a context whose tension moved while
the caller believes a hormone moved too -- a silent no-op that only shows up as
a behavioural anomaly much later.  Raising with the offending key in the
message turns the mistake into a stack trace at the call site instead.

**Why the hormones are not committed here at all.**  ADR-009 section S1 makes
``GlobalContext.pulse`` the property of a UseCase transaction that writes the
global pulse and the local snapshot together.  A second writer inside
``commit_delta`` would give the same field two owners, and the two-carrier
write would no longer be atomic.  Hormone increments therefore go through
``dynamics.update_hormones``, which is where the two carriers are reconciled.

**Why ``GlobalContext`` has no ``ipc`` field.**  That is a structural fact of
the contract, not an omission: IPC coordinates are per-user and live on
``S1Snapshot.ipc`` (``state.IPCState``), while ``GlobalContext`` is the one
object shared across every ``scope_id``.  An ``ipc.*`` delta reaching this
function therefore names a field that does not exist on the object it would
commit to -- another reason the rejection is mandatory.

The function is pure: it reads no clock, touches no IO and never mutates its
arguments.  ``last_feedback_at`` is carried through verbatim; committing a
delta is not the same event as receiving feedback, so it must not stamp that
anchor.
"""

from __future__ import annotations

from ..domain.global_context import GlobalContext, GlobalDelta

TENSION_FLOOR = 0.0
TENSION_CEILING = 100.0


def _reject_non_tension_carrier(carrier: dict[str, float], handler: str) -> None:
    """Raise if ``carrier`` holds anything, naming every offending key.

    An empty dict means "no increments in this carrier" and is not an error --
    a caller may build a delta with both carriers present but empty.
    """
    if not carrier:
        return
    keys = ", ".join(sorted(carrier))
    raise ValueError(
        f"commit_delta only commits tension and version; received {keys}. "
        f"Hormone and IPC increments belong to {handler}."
    )


def commit_delta(context: GlobalContext, delta: GlobalDelta) -> GlobalContext:
    """Return the next global context after applying ``delta``.

    Validates both carriers before any arithmetic, so a rejected delta leaves no
    trace on the returned value (there is nothing to roll back).
    """
    _reject_non_tension_carrier(delta.pulse, "dynamics.update_hormones")
    _reject_non_tension_carrier(delta.ipc, "dynamics.update_hormones")

    # Clamp before constructing: TensionState carries ge=0/le=100, so an
    # unclamped 103.0 would surface as a ValidationError from the round trip
    # rather than the committed pin the contract asks for.
    new_tension = min(
        TENSION_CEILING,
        max(TENSION_FLOOR, context.tension.value + delta.tension),
    )

    # Round trip through model_validate rather than model_copy(update=...):
    # model_copy skips validators entirely (scripts/check_model_copy.py enforces
    # this), and a skipped validator is exactly how an out-of-range value would
    # get stamped onto the context.
    return GlobalContext.model_validate(
        context.model_dump()
        | {
            "version": context.version + 1,
            "tension": {
                "value": new_tension,
                "last_feedback_at": context.tension.last_feedback_at,
            },
        }
    )
