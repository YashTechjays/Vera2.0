"""Normalizations applied to a value before it becomes a `field_answer` row."""

from vera_core.forms.answers import canonical_special_value, special_values_by_path
from vera_core.forms.catalog import SCHEMAS


def test_a_case_or_whitespace_variant_snaps_to_the_declared_literal() -> None:
    """Gates compare byte-exact, so "unlimited" would re-open met + remaining."""
    specials = ["$0", "None", "No Deductible", "Unlimited", "No Limit"]
    assert canonical_special_value(" unlimited ", specials) == "Unlimited"
    assert canonical_special_value("NO LIMIT", specials) == "No Limit"


def test_an_amount_matching_no_declared_literal_is_left_verbatim() -> None:
    """Snapping is confined to the declared vocabulary — currency shape is a separate,
    deliberately untreated concern, and storage IS the export."""
    assert canonical_special_value("$1,500.00", ["$0", "Unlimited"]) == "$1,500.00"
    assert canonical_special_value("$1,500.00", None) == "$1,500.00"


def test_the_catalog_gate_literals_are_reachable_by_path() -> None:
    """The map both extraction stacks build their snap from."""
    doc = SCHEMAS["infertility_treatment"][1]()
    by_path = special_values_by_path(doc)
    assert by_path["sections.deductibles.individual.total"] == [
        "$0",
        "None",
        "No Deductible",
        "Unlimited",
        "No Limit",
    ]
    assert "sections.deductibles.individual.met_amount" not in by_path
