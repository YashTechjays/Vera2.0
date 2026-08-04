"""Money-triplet consistency: currency parsing, partial data, violation reasons."""

from vera_core.forms.consistency import check_triplet, parse_currency, triplet_paths

BASE = "sections.lifetime_maximum"
TOTAL, MET, REMAINING = triplet_paths(BASE)


class TestParseCurrency:
    def test_parses_plain_and_formatted_amounts(self) -> None:
        assert parse_currency("300") == 300.0
        assert parse_currency("$1,500.50") == 1500.50
        assert parse_currency(" $25,000 ") == 25000.0

    def test_rejects_specials_prose_and_blanks(self) -> None:
        assert parse_currency("No Limit") is None
        assert parse_currency("Met") is None
        assert parse_currency("") is None
        assert parse_currency("call back later") is None

    def test_rejects_non_finite(self) -> None:
        assert parse_currency("inf") is None
        assert parse_currency("nan") is None


class TestCheckTriplet:
    def test_consistent_values_pass(self) -> None:
        answers = {TOTAL: "$25,000", MET: "$5,000", REMAINING: "$20,000"}
        assert check_triplet(BASE, answers) is None

    def test_met_exceeding_total_is_flagged_with_amounts(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$100", MET: "$300"})
        assert reason is not None
        assert "met amount ($300.00) exceeds the total ($100.00)" in reason

    def test_remaining_exceeding_total_is_flagged(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$100", REMAINING: "$300"})
        assert reason is not None
        assert "remaining amount ($300.00) exceeds the total ($100.00)" in reason

    def test_bug_report_example_flags_both_exceeds(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$100", MET: "$300", REMAINING: "$300"})
        assert reason is not None
        assert "met amount ($300.00) exceeds" in reason
        assert "remaining amount ($300.00) exceeds" in reason
        assert "does not match" not in reason  # exceed clauses suppress the sum clause

    def test_sum_mismatch_is_flagged_when_nothing_exceeds(self) -> None:
        reason = check_triplet(BASE, {TOTAL: "$25,000", MET: "$5,000", REMAINING: "$25,000"})
        assert reason is not None
        assert (
            "met amount ($5,000.00) plus the remaining amount ($25,000.00) "
            "does not match the total ($25,000.00)" in reason
        )

    def test_one_cent_rounding_is_tolerated(self) -> None:
        assert check_triplet(BASE, {TOTAL: "100", MET: "50", REMAINING: "50.01"}) is None
        assert check_triplet(BASE, {TOTAL: "100", MET: "50", REMAINING: "50.02"}) is not None

    def test_partial_data_never_fires(self) -> None:
        assert check_triplet(BASE, {}) is None
        assert check_triplet(BASE, {TOTAL: "$100"}) is None
        assert check_triplet(BASE, {MET: "$300", REMAINING: "$300"}) is None  # no total
        assert check_triplet(BASE, {TOTAL: "$100", MET: "$50"}) is None  # sum needs all 3

    def test_special_values_do_not_participate(self) -> None:
        assert check_triplet(BASE, {TOTAL: "No Limit", MET: "$300", REMAINING: "$300"}) is None
        assert check_triplet(BASE, {TOTAL: "$100", MET: "$50", REMAINING: "Met"}) is None
