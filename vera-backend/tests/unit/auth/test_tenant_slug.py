"""Unit tests for the pure slug helpers. The DB-backed `resolve_tenant_id` (the
SECURITY DEFINER lookup) is exercised by the integration login suite."""

import pytest

from control_plane.auth.tenant_slug import is_valid_slug, normalize_slug


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Vera-Health  ", "vera-health"),
        ("ACME", "acme"),
        ("already-good", "already-good"),
    ],
)
def test_normalize_lowercases_and_strips(raw: str, expected: str) -> None:
    assert normalize_slug(raw) == expected


@pytest.mark.parametrize(
    "slug",
    [
        "a",
        "ab",
        "acme",
        "vera-health-example",
        "tenant-123",
        "0190abcd-1234-7000-8000-000000000000",  # a UUID string is a valid slug
        "a" * 63,
    ],
)
def test_valid_slugs(slug: str) -> None:
    assert is_valid_slug(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "-leading",
        "trailing-",
        "Upper",  # not normalized
        "white space",
        "under_score",
        "dot.dot",
        "a" * 64,  # too long
    ],
)
def test_invalid_slugs(slug: str) -> None:
    assert not is_valid_slug(slug)
