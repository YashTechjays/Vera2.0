"""Tests for the schema-driven {{token}} resolvers (vera_core.forms.placeholders)."""

import pytest

from vera_core.forms.catalog.ibv_standard import build_ibv_standard
from vera_core.forms.placeholders import (
    UnknownPlaceholderError,
    placeholder_tokens,
    resolve_field_path,
    resolve_prompt,
)

_DOC = build_ibv_standard()


def test_resolve_field_path_prefers_system_fields() -> None:
    # A system_fields handle resolves to its declared path.
    assert resolve_field_path(_DOC, "member_id") == "sections.insurance_information.policy_number"
    assert resolve_field_path(_DOC, "doctor_npi") == "sections.provider_reference_information.npi"
    assert resolve_field_path(_DOC, "hospital_tax_id") == "sections.hospital_information.tax_id"


def test_resolve_field_path_falls_back_to_context_leaf_path() -> None:
    # A context-role leaf is addressed by its own path (the token IS the path).
    path = "sections.patient_information.patient_name"  # patient_name leaf is role="context"
    assert resolve_field_path(_DOC, path) == path


def test_resolve_field_path_unknown_token_raises() -> None:
    # An unknown token (misauthored / not defined by the schema) fails early instead of silently
    # resolving to nothing — group_number is a collected `ask` leaf, not a system_field/context.
    with pytest.raises(UnknownPlaceholderError):
        resolve_field_path(_DOC, "group_number")
    with pytest.raises(UnknownPlaceholderError):
        resolve_field_path(_DOC, "not_a_real_token")


def test_placeholder_tokens_covers_handles_and_context_paths() -> None:
    tokens = placeholder_tokens(_DOC)
    assert {"member_id", "doctor_npi", "hospital_tax_id"} <= tokens  # system_fields handles
    assert "sections.patient_information.patient_name" in tokens  # a context-leaf path
    assert "group_number" not in tokens  # collected ask leaf, not a placeholder


def test_resolve_prompt_fills_known_tokens_and_drops_unknown() -> None:
    values = {"member_id": "POL-661522"}
    out = resolve_prompt("member ID {{member_id}}, group {{group_number}} end", values.get)
    # a resolvable token is filled; an unresolved one collapses to empty (caller supplies defaults)
    assert out == "member ID POL-661522, group  end"


def test_resolve_prompt_leaves_non_placeholder_text_intact() -> None:
    assert resolve_prompt("press 1 or 2, say Medical", {}.get) == "press 1 or 2, say Medical"
