"""Unit tests for the UI-replica export sheet — pure, no DB."""

from __future__ import annotations

from typing import Any

from vera_core.forms.dsl import FormSchemaDoc
from vera_core.forms.export_form_sheet import section_table

# A minimal v2 doc with one table section exercising BOTH group shapes:
#  - ivf: subgroup rows (cpt_1, cpt_2) + group-level extras (cycle_limit, notes)
#  - oi:  leaf-only group (its leaves split into row cells vs extras by key)
_TABLE_DOC: dict[str, Any] = {
    "dsl_version": "2.1",
    "name": "Replica Test Schema",
    "insurance_type": "infertility_treatment",
    "system_fields": {"in_network": "sections.info.in_network"},
    "rep_call_reference_number_field": "sections.info.in_network",
    "promoted_fields": {},  # filled in fixture below
    "sections": {
        "info": {
            "title": "Info",
            "role": "collect",
            "fields": {
                "in_network": {
                    "type": "text",
                    "title": "In Network",
                    "role": "ask",
                    "required": True,
                    "prompt": {"ask": "In network?"},
                },
            },
        },
        "treatment": {
            "title": "Treatment",
            "role": "collect",
            "ui": {"layout": "table"},
            "fields": {
                "tx_covered": {
                    "type": "text",
                    "title": "Treatment Covered",
                    "role": "ask",
                    "prompt": {"ask": "Covered?"},
                },
                "ivf": {
                    "type": "group",
                    "title": "In Vitro Fertilization (IVF)",
                    "codes": {"icd10": ["Z31.83"]},
                    "fields": {
                        "cycle_limit": {
                            "type": "text",
                            "title": "Cycle Limit",
                            "role": "ask",
                            "prompt": {"ask": "Cycle limit?"},
                        },
                        "notes": {
                            "type": "text",
                            "title": "Additional Notes",
                            "role": "ask",
                            "prompt": {"ask": "Notes?"},
                        },
                        "cpt_58970": {
                            "type": "group",
                            "title": "CPT 58970",
                            "fields": {
                                "covered": {
                                    "type": "text",
                                    "title": "Covered",
                                    "role": "ask",
                                    "prompt": {"ask": "Covered?"},
                                },
                                "copay": {
                                    "type": "text",
                                    "title": "Copay ($)",
                                    "role": "ask",
                                    "prompt": {"ask": "Copay?"},
                                },
                            },
                        },
                        "cpt_89280": {
                            "type": "group",
                            "title": "CPT 89280",
                            "fields": {
                                "covered": {
                                    "type": "text",
                                    "title": "Covered",
                                    "role": "ask",
                                    "prompt": {"ask": "Covered?"},
                                },
                                "copay": {
                                    "type": "text",
                                    "title": "Copay ($)",
                                    "role": "ask",
                                    "prompt": {"ask": "Copay?"},
                                },
                            },
                        },
                    },
                },
                "oi": {
                    "type": "group",
                    "title": "Ovulation Induction",
                    "codes": {"icd10": ["N97.0"], "cpt": ["58323"]},
                    "fields": {
                        "covered": {
                            "type": "text",
                            "title": "Covered",
                            "role": "ask",
                            "prompt": {"ask": "Covered?"},
                        },
                        "copay": {
                            "type": "text",
                            "title": "Copay ($)",
                            "role": "ask",
                            "prompt": {"ask": "Copay?"},
                        },
                        "cycle_limit": {
                            "type": "text",
                            "title": "Cycle Limit",
                            "role": "ask",
                            "prompt": {"ask": "Cycle limit?"},
                        },
                    },
                },
            },
        },
    },
    "tasks": [{"task_key": "main", "title": "Main", "sections": ["info", "treatment"]}],
}


def _doc() -> FormSchemaDoc:
    from vera_core.forms.dsl import PromotedFields

    raw = dict(_TABLE_DOC)
    raw["promoted_fields"] = dict.fromkeys(PromotedFields.model_fields, "sections.info.in_network")
    return FormSchemaDoc.model_validate(raw)


def test_section_table_none_for_non_table_sections() -> None:
    doc = _doc()
    assert section_table("info", doc.sections["info"]) is None


def test_section_table_model_matches_frontend_rules() -> None:
    doc = _doc()
    table = section_table("treatment", doc.sections["treatment"])
    assert table is not None

    # Section-level leaves render above the grid.
    assert [p for p, _ in table.leaves] == ["sections.treatment.tx_covered"]

    # Columns = shared leaf keys of subgroup rows, first-seen order.
    assert [key for key, _ in table.columns] == ["covered", "copay"]
    assert [t for _, t in table.columns] == ["Covered", "Copay ($)"]
    # Extras = leaf keys sitting beside subgroups inside any group.
    assert [key for key, _ in table.extra_columns] == ["cycle_limit", "notes"]
    assert table.has_icd is True

    ivf, oi = table.groups
    assert ivf.label == "In Vitro Fertilization (IVF)"
    assert ivf.icd10 == "Z31.83"
    assert [r.label for r in ivf.rows] == ["CPT 58970", "CPT 89280"]
    assert ivf.rows[0].cells["copay"].path == ("sections.treatment.ivf.cpt_58970.copay")
    assert set(ivf.extras) == {"cycle_limit", "notes"}

    # Leaf-only group: the group itself is one row labelled by its CPT codes;
    # keys that exist as extras elsewhere split out of the row cells.
    assert oi.label == "Ovulation Induction"
    assert len(oi.rows) == 1
    assert oi.rows[0].label == "58323"
    assert set(oi.rows[0].cells) == {"covered", "copay"}
    assert set(oi.extras) == {"cycle_limit"}
