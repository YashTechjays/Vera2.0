"""`decide_retry` — the park-vs-redial rule, in one place and fully deterministic.

There is exactly ONE caller (`post_call_eval.evaluate_call`) precisely so this cannot drift
again: the rule previously lived inline there AND, in a thinner form, in
`control_plane.post_call.resolve_ai_processing`, which shared the threshold number but not the
askability guards — so a form whose only remaining gaps were unaskable parked on one path and
redialed on the other.

Every input is a plain bool or an enum, so the whole rule is exhaustively testable without a
session, a schema, or a call.
"""

from __future__ import annotations

import itertools

import pytest

from vera_core.models.enums import ReviewReason
from vera_core.services.retry_decision import Park, Redial, decide_retry

# The state that redials: something is unsatisfied, the fill is below the threshold, the tail
# is reachable, retries remain, nothing forbids a redial, and both auto-retry gates are on.
REDIAL_CASE: dict[str, object] = {
    "unsatisfied": True,
    "retryable": True,
    "fraction_below_threshold": True,
    "no_retry": None,
    "can_retry": True,
    "auto_retry_allowed": True,
}


def test_the_only_redial_state() -> None:
    assert decide_retry(**REDIAL_CASE) == Redial()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("flip", "value", "reason"),
    [
        # Nothing left to ask: park for sign-off, never redial. Checked FIRST, so a fully
        # satisfied form reads READY_FOR_REVIEW and not FILL_THRESHOLD_MET.
        ("unsatisfied", False, ReviewReason.READY_FOR_REVIEW),
        # Good enough: the call verified the tenant's threshold, so don't redial for the tail.
        ("fraction_below_threshold", False, ReviewReason.FILL_THRESHOLD_MET),
        # THE BUG THIS FUNCTION EXISTS FOR. Gaps remain and the fill is low, but nothing
        # remaining is askable — defaulted or non-collectable leaves no call could ever fill.
        # The fallback resolver lacked this guard and would redial a real payer to ask
        # questions the schema says cannot be asked.
        ("retryable", False, ReviewReason.UNSATISFIED_UNASKABLE),
        # Budget spent.
        ("can_retry", False, ReviewReason.RETRIES_EXHAUSTED),
        # Either gate off: the deployment kill-switch, the tenant's own switch, or both.
        ("auto_retry_allowed", False, ReviewReason.AUTO_RETRY_DISABLED),
    ],
)
def test_each_guard_alone_turns_the_redial_into_a_park(
    flip: str, value: bool, reason: ReviewReason
) -> None:
    """One flip at a time from the redial state, so each guard is shown to be independently
    load-bearing rather than passing because a neighbour already parked."""
    assert decide_retry(**{**REDIAL_CASE, flip: value}) == Park(reason)  # type: ignore[arg-type]


def test_a_never_redial_call_parks_with_its_own_reason() -> None:
    """A supervisor-ended or rule-terminated call is never auto-redialed whatever the fill,
    and the reviewer must see WHY it stopped — not a generic fill reason."""
    for reason in (ReviewReason.USER_ENDED, ReviewReason.TERMINATED_BY_RULE):
        assert decide_retry(**{**REDIAL_CASE, "no_retry": reason}) == Park(reason)  # type: ignore[arg-type]


def test_retries_exhausted_beats_unaskable_only_when_the_tail_is_reachable() -> None:
    """The two exhausted-ish reasons are not interchangeable: RETRIES_EXHAUSTED means a retry
    WOULD have helped but the budget is gone; UNSATISFIED_UNASKABLE means no retry could ever
    have helped. A reviewer triages those differently."""
    spent = {**REDIAL_CASE, "can_retry": False}
    assert decide_retry(**spent) == Park(ReviewReason.RETRIES_EXHAUSTED)  # type: ignore[arg-type]
    assert decide_retry(**{**spent, "retryable": False}) == Park(  # type: ignore[arg-type]
        ReviewReason.UNSATISFIED_UNASKABLE
    )


def test_it_is_total_and_never_redials_outside_the_one_state() -> None:
    """Exhaustive over all 2^5 boolean combinations x the no_retry axis: the function always
    returns a decision, and Redial appears for EXACTLY the one input state. That is the
    property that makes a second implementation unnecessary — and its absence is what let the
    fallback resolver redial in states this rule parks."""
    redials = 0
    for combo in itertools.product([True, False], repeat=5):
        for no_retry in (None, ReviewReason.USER_ENDED):
            kwargs = dict(
                zip(
                    (
                        "unsatisfied",
                        "retryable",
                        "fraction_below_threshold",
                        "can_retry",
                        "auto_retry_allowed",
                    ),
                    combo,
                    strict=True,
                )
            )
            decision = decide_retry(**kwargs, no_retry=no_retry)
            assert isinstance(decision, Park | Redial)
            if isinstance(decision, Redial):
                redials += 1
                assert no_retry is None
                assert kwargs == {
                    "unsatisfied": True,
                    "retryable": True,
                    "fraction_below_threshold": True,
                    "can_retry": True,
                    "auto_retry_allowed": True,
                }
    assert redials == 1, "exactly one of the 64 input states may redial"
