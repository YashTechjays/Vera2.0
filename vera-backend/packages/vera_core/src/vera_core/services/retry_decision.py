"""The park-vs-redial decision, in one place for both post-call resolvers.

Two consumers close a call and must answer the same question — park this form for human
review, or redial the payer — and which one runs depends only on HOW the call closed, never
on anything about the form:

* ``post_call_eval.evaluate_call`` — the eval path, when the judge is configured;
* ``control_plane.post_call.resolve_ai_processing`` — the fallback, when it is not, or when
  the sweeper reclaims a stranded form.

They previously shared the NUMBER but not the DECISION. The fallback compared the same
verified fraction against the same threshold, but lacked two guards the eval path applies:
"nothing is unsatisfied" and "nothing unsatisfied is even askable". So a form whose only
remaining gaps were unaskable — defaulted or non-collectable leaves no call could ever fill —
parked on the eval path and REDIALED on the fallback, dialing a real payer to ask questions
the schema says cannot be asked. Copying the guards across is what produced the drift in the
first place (the number was copied, the guards were left behind), so the whole decision lives
here instead, as a pure function with no session and no ORM.

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
