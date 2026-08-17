"""Canonicalization of an answer against the literals its leaf authors."""

import pytest

from vera_core.forms.answers import (
    LeafLiterals,
    canonical_answer,
    canonicalize_answers,
    leaf_literals,
    literals_of,
    spoken_literals,
)
from vera_core.forms.catalog import SCHEMAS
from vera_core.forms.dsl import FormSchemaDoc, Leaf
from vera_core.forms.intake import enum_accepted_values

_TOTAL_SPECIALS = ("$0", "None", "No Deductible", "Unlimited", "No Limit")
_TOTAL_PATH = "sections.deductibles.individual.total"
_MONEY = LeafLiterals(gate=_TOTAL_SPECIALS, spoken=_TOTAL_SPECIALS, money=True)
_YES_NO_NA = LeafLiterals(gate=("Yes", "No", "N/A"), spoken=(), money=False)


@pytest.fixture(scope="module")
def doc() -> FormSchemaDoc:
    return SCHEMAS["infertility_treatment"][1]()


def test_a_case_or_whitespace_variant_snaps_to_the_declared_literal() -> None:
    """Gates compare byte-exact, so "unlimited" would re-open met + remaining."""
    assert canonical_answer(" unlimited ", _MONEY) == "Unlimited"
    assert canonical_answer("NO LIMIT", _MONEY) == "No Limit"


def test_a_money_variant_of_the_zero_sentinel_snaps_too() -> None:
    """Folding case and padding alone leaves the likeliest spellings of a $0 deductible off
    the sentinel, so met + remaining go on being asked for a deductible that is nothing."""
    assert canonical_answer("$0.00", _MONEY) == "$0"
    assert canonical_answer("0", _MONEY) == "$0"


def test_the_money_comparison_is_confined_to_currency_leaves() -> None:
    """`parse_currency` strips `$`, `,` and `%`, so on a text or integer leaf it would call
    two different answers the same — only a currency leaf means "$0.00" == "$0"."""
    text = LeafLiterals(gate=_TOTAL_SPECIALS, spoken=_TOTAL_SPECIALS, money=False)
    assert canonical_answer("$0.00", text) == "$0.00"


def test_an_amount_matching_no_declared_literal_is_left_verbatim() -> None:
    """Snapping is confined to the declared vocabulary — currency shape is a separate,
    deliberately untreated concern, and storage IS the export."""
    assert canonical_answer("$1,500.00", _MONEY) == "$1,500.00"
    assert canonical_answer("$1,500.00", None) == "$1,500.00"


def test_padding_is_stripped_even_from_an_answer_that_matches_nothing() -> None:
    """The model pads its values, and nothing downstream strips them — an `eq` gate on a
    padded "Yes " is false, so the whole sub-panel it guards is read as inapplicable."""
    assert canonical_answer(" Yes ", None) == "Yes"
    assert canonical_answer(" 3 visits ", _MONEY) == "3 visits"


def test_a_non_ascii_space_is_not_treated_as_padding() -> None:
    """`review` deliberately strips ASCII whitespace only, so a non-ASCII space stays a real
    value difference — this must not quietly hold a second, broader rule."""
    assert canonical_answer("\u00a0Yes\u00a0", None) == "\u00a0Yes\u00a0"


def test_a_non_string_answer_passes_through() -> None:
    """Intake may post a number or a bool; `review.normalize_value` leaves those alone too."""
    assert canonical_answer(12, _MONEY) == 12
    assert canonical_answer(None, _MONEY) is None


def test_an_enums_own_values_are_snapped_as_well() -> None:
    """`eq(f"{base}.covered", "Yes")` gates copay, coinsurance and prior_auth — a lowercase
    "yes" would silently mark all three inapplicable."""
    assert canonical_answer("yes", _YES_NO_NA) == "Yes"
    assert canonical_answer("n/a", _YES_NO_NA) == "N/A"


def test_an_enum_names_nothing_for_an_extraction_prompt_to_spell() -> None:
    """Its own `(one of: …)` clause carries the vocabulary, and the `inapplicable_value` in
    `gate` is written by the either/or auto-fill — never something a representative says."""
    enum = literals_of(
        Leaf(
            type="enum",
            title="Referral",
            role="input",
            values=["Yes", "No"],
            inapplicable_value="N/A",
        )
    )
    assert enum.gate == ("Yes", "No", "N/A")
    assert enum.spoken == ()


def test_the_catalog_gate_literals_are_reachable_by_path(doc: FormSchemaDoc) -> None:
    """The one map every answer writer and gate baseline builds its snap from."""
    by_path = leaf_literals(doc)
    assert by_path[_TOTAL_PATH].gate == _TOTAL_SPECIALS
    assert by_path[_TOTAL_PATH].money is True
    assert "sections.deductibles.individual.met_amount" not in by_path


def test_the_prompt_slice_drops_every_enum(doc: FormSchemaDoc) -> None:
    spoken = spoken_literals(leaf_literals(doc))
    assert spoken[_TOTAL_PATH] == _TOTAL_SPECIALS
    assert "sections.benefit_coverage.pcp_referral_required" not in spoken


def test_intake_accepts_exactly_what_a_gate_recognizes(doc: FormSchemaDoc) -> None:
    """One union, two readers — a value intake accepts but a gate does not recognize is a
    question nothing can close."""
    literals = leaf_literals(doc)
    for path, accepted in enum_accepted_values(doc).items():
        assert accepted == set(literals[path].gate)


def test_canonicalize_answers_covers_a_flattened_intake_list(doc: FormSchemaDoc) -> None:
    assert canonicalize_answers([(_TOTAL_PATH, "no limit")], doc) == [(_TOTAL_PATH, "No Limit")]
