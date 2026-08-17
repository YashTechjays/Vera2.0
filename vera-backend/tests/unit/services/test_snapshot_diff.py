from vera_core.services.call_provenance import snapshot_changed_paths


def test_changed_added_removed_paths_sorted() -> None:
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "9", "d": "4"}  # b changed, c removed, d added
    assert snapshot_changed_paths(before, after) == ["b", "c", "d"]


def test_none_and_missing_snapshots_are_empty() -> None:
    assert snapshot_changed_paths(None, None) == []
    assert snapshot_changed_paths({}, {}) == []


def test_absent_key_differs_from_present_none() -> None:
    # A key whose value is None is still "present" — only true absence/difference counts.
    assert snapshot_changed_paths({"a": None}, {"a": None}) == []
    assert snapshot_changed_paths({}, {"a": None}) == ["a"]


def test_unfinalized_after_state_yields_no_diff() -> None:
    # after_state stays {} until the post-call eval fills it — a live or
    # never-evaluated call must not report every pre-existing field as changed.
    assert snapshot_changed_paths({"a": "1", "b": "2"}, {}) == []
