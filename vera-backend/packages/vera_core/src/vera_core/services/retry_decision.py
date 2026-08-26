"""The park-vs-redial decision — park this form for human review, or redial the payer.

ONE caller: ``post_call_eval.evaluate_call``. It was extracted while there were two, and the
extraction is what proved the second could not make this decision at all — the fallback
resolver runs precisely when no judge ran, ``is_call_confirmed`` requires a judge verdict, so
the verified fraction was structurally 0.0 there for every call however good, and 0.0 is below
every threshold. With auto-retry on it redialed every form until ``max_retries``. It now makes
no fill-based decision, leaving this module a single consumer.

Kept as a module rather than folded back inline because the drift it was extracted to fix came
from copying the NUMBER between two sites and leaving the GUARDS behind (a form whose only gaps
were unaskable parked on one path and REDIALED on the other, dialing a payer to ask questions
the schema says cannot be asked). A pure function with no session and no ORM is testable
against that whole decision table without a database, which is what pins the guards together.

`token_fields` deliberately stays in `evaluate_call`: a tokenized value is an extraction
artifact of that path, not a retry consideration.
"""

from __future__ import annotations

from dataclasses import dataclass

from vera_core.models.enums import ReviewReason

__all__ = ["Park", "Redial", "RetryDecision", "decide_retry"]


@dataclass(frozen=True)
class Park:
    """Route the form to EXCEPTION_REVIEW, with the reason a reviewer will read."""

    reason: ReviewReason


@dataclass(frozen=True)
class Redial:
    """Re-queue the form for another call."""


RetryDecision = Park | Redial


def decide_retry(
    *,
    unsatisfied: bool,
    retryable: bool,
    fraction_below_threshold: bool,
    no_retry: ReviewReason | None,
    can_retry: bool,
    auto_retry_allowed: bool,
) -> RetryDecision:
    """Park or redial. Pure — every input is already resolved by the caller.

    Ordered exactly as `evaluate_call` applied these rules before the extraction, because the
    order is load-bearing: the "good enough" threshold check must come AFTER "nothing is
    unsatisfied" (a fully satisfied form parks as READY_FOR_REVIEW, not FILL_THRESHOLD_MET)
    and BEFORE the askability check (a form over the threshold parks without ever asking
    whether its tail is reachable).

    Booleans rather than the collections themselves: the caller owns the PHI-bearing values
    and the paths, and this function needs only their emptiness. Keeping it value-free means
    it can never log or leak one.

    *no_retry* is `call_lifecycle.no_retry_reason` — a supervisor-ended or rule-terminated
    call is never auto-redialed whatever the fill.
    """
    if not unsatisfied:
        return Park(ReviewReason.READY_FOR_REVIEW)
    if not fraction_below_threshold:
        # Good-enough gate: the call verified the tenant's threshold of the applicable
        # required fields, so park for review rather than redial for the tail.
        return Park(ReviewReason.FILL_THRESHOLD_MET)
    if retryable and can_retry:
        if no_retry is not None:
            return Park(no_retry)
        if auto_retry_allowed:
            return Redial()
        # Either gate off: the deployment kill-switch, the tenant's own switch, or both.
        return Park(ReviewReason.AUTO_RETRY_DISABLED)
    return Park(ReviewReason.RETRIES_EXHAUSTED if retryable else ReviewReason.UNSATISFIED_UNASKABLE)
