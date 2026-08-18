"""Pin the date-shape rule both extractors send (`forms/extraction_prompt`).

`plan_year_information` is a `text` leaf holding a date RANGE, so no `date_format` reaches
it from the schema and neither the frontend regex nor `normalize_date_answers` can rescue a
bad shape — the rule text is the whole defence, and these tests are what stop it drifting
away from the literal the field's own auto-fill twin writes.
"""

from datetime import date

from vera_core.forms.authoring import DATE_VALIDATION
from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.dsl import parse_date_format
from vera_core.forms.extraction_prompt import ANSWER_DATE_FORMAT_RULE

_RULE_FORMAT = "MM/DD/YYYY"
_PLAN_YEAR = "sections.benefit_coverage.plan_year_information"


def test_the_rule_targets_the_shape_the_auto_fill_twin_writes() -> None:
    """One leaf, two writers: the extractor on the Plan Year branch and the derive on the
    Calendar Year branch. Told a different shape than the derive literal, the extractor makes
    the column hold both — the defect the shared module exists to prevent."""
    leaf = dict(build_ibv_standard().leaf_items())[_PLAN_YEAR]
    assert leaf.derive is not None
    halves = leaf.derive.value.replace("{{current_year}}", "2026").split(" - ")

    assert len(halves) == 2  # the rule's " - " join is the derive literal's join
    for half in halves:
        assert parse_date_format(half, _RULE_FORMAT) is not None


def test_the_padded_shape_is_still_legal_on_a_date_typed_leaf() -> None:
    """The rule says MM/DD/YYYY, every date leaf declares M/D/YYYY. That is deliberate — the
    derive literal and the seeds are padded — and safe only because `M` accepts 1-2 digits.

    The probe is month≠day on purpose: 01/01 parses identically under M/D, MM/DD and D/M, so
    a palindromic date would keep this green even if the declared format went day-first, and
    every AI-written date whose parts are both ≤ 12 would silently transpose.
    """
    declared = DATE_VALIDATION.date_format
    assert declared is not None
    assert parse_date_format("01/02/2026", declared) == date(2026, 1, 2)


def test_the_rule_stays_scoped_to_dates_and_names_its_shape() -> None:
    """Generalized into "write only the answer, not the sentence" it would truncate
    `additional_notes`, a leaf whose correct answer IS prose. It also must stay free of "$":
    the top-up prompt asserts money is never mentioned (currency has no normalizer)."""
    assert "a date answer" in ANSWER_DATE_FORMAT_RULE
    assert _RULE_FORMAT in ANSWER_DATE_FORMAT_RULE
    assert '" - "' in ANSWER_DATE_FORMAT_RULE
    assert "$" not in ANSWER_DATE_FORMAT_RULE
