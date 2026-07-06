"""The authoring catalog's new guarantees, checked at the metadata level (no DB):
the InsuranceType CHECK, the one-family-per-type UNIQUE, and the one-published-
version-per-schema/prompt partial indexes — plus that the seed data files parse."""

import json
from pathlib import Path

from vera_core.models.authoring import FormSchema, PromptVersion, SchemaVersion
from vera_core.models.enums import InsuranceType, check_in

FORM_SCHEMA_DIR = Path(__file__).parents[3] / "data" / "form_schemas"
PROMPT_DIR = Path(__file__).parents[3] / "data" / "prompts"


def test_insurance_type_values() -> None:
    assert [t.value for t in InsuranceType] == ["infertility_treatment", "disease_only"]


def test_check_in_renders_insurance_type_check() -> None:
    constraint = check_in("insurance_type", InsuranceType)
    assert constraint.name == "insurance_type_valid"
    assert str(constraint.sqltext) == (
        "insurance_type IN ('infertility_treatment', 'disease_only')"
    )


def test_form_schema_has_check_and_unique() -> None:
    table = FormSchema.metadata.tables["form_schema"]
    names = {c.name for c in table.constraints}
    assert "ck_form_schema_insurance_type_valid" in names
    assert "uq_form_schema_insurance_type" in names


def test_schema_version_has_published_partial_index() -> None:
    table = SchemaVersion.metadata.tables["schema_version"]
    index = next(i for i in table.indexes if i.name == "uq_schema_version_published_per_schema")
    assert index.unique
    assert "status = 'published'" in str(index.dialect_options["postgresql"]["where"])


def test_prompt_version_has_published_partial_index() -> None:
    table = PromptVersion.metadata.tables["prompt_version"]
    index = next(i for i in table.indexes if i.name == "uq_prompt_version_published_per_prompt")
    assert index.unique
    assert "status = 'published'" in str(index.dialect_options["postgresql"]["where"])


def test_manifest_and_schema_files_parse() -> None:
    manifest = json.loads((FORM_SCHEMA_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest, "manifest must list at least one schema"
    valid_types = {t.value for t in InsuranceType}
    for entry in manifest:
        assert entry["insurance_type"] in valid_types
        doc = json.loads((FORM_SCHEMA_DIR / entry["file"]).read_text(encoding="utf-8"))
        assert doc["name"], "schema document must carry a top-level name"


def test_manifest_and_prompt_files_parse() -> None:
    # Guards the seed loader's doc["name"] / entry["insurance_type"] access — a
    # malformed prompt file would otherwise only surface as a crash at seed time.
    manifest = json.loads((PROMPT_DIR / "manifest.json").read_text())
    assert manifest, "manifest must list at least one prompt"
    valid_types = {t.value for t in InsuranceType}
    for entry in manifest:
        assert entry["insurance_type"] in valid_types
        doc = json.loads((PROMPT_DIR / entry["file"]).read_text(encoding="utf-8"))
        assert doc["name"], "prompt document must carry a top-level name"
