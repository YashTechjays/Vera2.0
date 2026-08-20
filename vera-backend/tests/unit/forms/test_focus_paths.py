"""`is_call_confirmed` — did an AUTHORITATIVE call collect this, judge-supported?"""

from uuid import UUID, uuid4

from vera_core.forms.review import FieldStatus, is_call_confirmed, is_field_satisfied

AUTH, OTHER = uuid4(), uuid4()
CALLS = frozenset({AUTH})


def _status(
    source: str,
    *,
    supported: bool | None = True,
    confidence: int = 95,
    call_id: UUID = AUTH,
) -> FieldStatus:
    return FieldStatus(
        source=source, ai_supported=supported, ai_confidence=confidence, call_id=call_id
    )


class TestIsCallConfirmed:
    def test_authoritative_call_supported_answer_is_confirmed(self) -> None:
        assert is_call_confirmed(_status("ai_call"), authoritative_calls=CALLS, floor=70)

    def test_answer_from_a_non_authoritative_call_is_not(self) -> None:
        """The rep answered, but nothing ties the conversation to a payer record."""
        assert not is_call_confirmed(
            _status("ai_call", call_id=OTHER), authoritative_calls=CALLS, floor=70
        )

    def test_intake_value_is_not_confirmed_even_though_it_is_satisfied(self) -> None:
        """The divergence from `is_field_satisfied` that this whole predicate exists for."""
        intake = FieldStatus(source="intake", ai_supported=None, ai_confidence=None, call_id=None)
        assert is_field_satisfied(intake, floor=70) is True
        assert is_call_confirmed(intake, authoritative_calls=CALLS, floor=70) is False

    def test_human_value_is_not_confirmed(self) -> None:
        human = FieldStatus(source="human", ai_supported=None, ai_confidence=None, call_id=None)
        assert not is_call_confirmed(human, authoritative_calls=CALLS, floor=70)

    def test_judge_rejected_answer_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(
            _status("ai_call", supported=False, confidence=38), authoritative_calls=CALLS, floor=70
        )

    def test_below_floor_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(
            _status("ai_call", confidence=69), authoritative_calls=CALLS, floor=70
        )

    def test_unjudged_answer_is_not_confirmed(self) -> None:
        """No `field_evaluation` row yet — `ai_supported` is None, so nothing is proven."""
        assert not is_call_confirmed(
            _status("ai_call", supported=None), authoritative_calls=CALLS, floor=70
        )

    def test_absent_status_is_not_confirmed(self) -> None:
        assert not is_call_confirmed(None, authoritative_calls=CALLS, floor=70)
