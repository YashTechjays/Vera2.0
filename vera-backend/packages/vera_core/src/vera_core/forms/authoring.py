"""Authoring macros for the form-schema DSL.

These helpers exist only at authoring time — the compiler inlines everything, so the
compiled artifact stays free of library indirection (the `constraint_library` lesson).
Reusable *shapes* (the covered/copay/coinsurance/prior-auth service item, per-CPT
groups, money triplets) live here; reusable *predicates* stay in the document's
``shared_conditions``.
"""

from __future__ import annotations

from typing import Literal

from vera_core.forms.dsl import (
    Alternatives,
    AnyCondition,
    Codes,
    Comparison,
    Condition,
    FieldPrompt,
    FormField,
    Group,
    Leaf,
    Range,
    RefCondition,
    RequiredWhen,
    Ui,
    Validation,
)

YES_NO = ["Yes", "No"]
YES_NO_NA = ["Yes", "No", "N/A"]

# Every date field currently shares one entry/display format (product decision);
# `text_ask(type_="date")` applies it automatically, raw Leaf sites use the constant.
DATE_VALIDATION = Validation(date_format="M/D/YYYY")

PRIOR_AUTH_ASK = "Is prior authorization required for this service? Please answer Yes, No, or N/A."
COPAY_ASK = "What is the copay amount for this service?"
COINSURANCE_ASK = "What is the coinsurance percentage for this service?"

Flavor = Literal["treatment", "male", "plain"]
_INAPPLICABLE: dict[Flavor, dict[str, str]] = {
    "treatment": {"covered": "No", "copay": "$0", "coinsurance": "0%", "prior_auth": "N/A"},
    "male": {"covered": "N/A", "copay": "N/A", "coinsurance": "N/A", "prior_auth": "N/A"},
    "plain": {},
}


def eq(field: str, value: str) -> Comparison:
    return Comparison(field=field, op="eq", value=value)


def not_in(field: str, values: list[str]) -> Comparison:
    return Comparison(field=field, op="not_in", value=values)


def any_of(*conditions: Condition) -> AnyCondition:
    return AnyCondition(any=list(conditions))


def ref(name: str) -> RefCondition:
    return RefCondition(ref=name)


def ask(text: str, hints: list[str] | None = None) -> FieldPrompt:
    return FieldPrompt(ask=text, hints=hints)


def service_fields(
    base: str,
    covered_ask: str,
    flavor: Flavor,
) -> dict[str, FormField]:
    """The standard covered/copay/coinsurance/prior_auth service item.

    ``base`` is the root-anchored path of the containing group; sub-field gates are
    wired to ``{base}.covered``. ``flavor`` picks the skip-fill defaults.
    """
    inapplicable = _INAPPLICABLE[flavor]
    gate = eq(f"{base}.covered", "Yes")
    return {
        "covered": Leaf(
            type="enum",
            title="Covered",
            role="ask",
            values=YES_NO_NA,
            required=True,
            inapplicable_value=inapplicable.get("covered"),
            prompt=ask(covered_ask),
        ),
        "copay": Leaf(
            type="currency",
            title="Copay ($)",
            role="ask",
            required=True,
            special_values=["$0", "None"],
            inapplicable_value=inapplicable.get("copay"),
            validation=Validation(range=Range(min=0)),
            applicable_when=gate,
            prompt=ask(COPAY_ASK),
        ),
        "coinsurance": Leaf(
            type="percent",
            title="Coinsurance (%)",
            role="ask",
            required=True,
            inapplicable_value=inapplicable.get("coinsurance"),
            validation=Validation(range=Range(min=0, max=100)),
            applicable_when=gate,
            prompt=ask(COINSURANCE_ASK),
        ),
        "prior_auth": Leaf(
            type="enum",
            title="Prior Authorization Required",
            role="ask",
            values=YES_NO_NA,
            required=True,
            special_values=["Prior auth department"],
            tags=["prior_auth"],
            inapplicable_value=inapplicable.get("prior_auth"),
            applicable_when=gate,
            prompt=ask(PRIOR_AUTH_ASK),
        ),
    }


def cpt_group(
    parent_base: str, code: str, flavor: Flavor, *, applicable_when: Condition | None = None
) -> Group:
    """A per-CPT-code service item under ``parent_base`` (group key = ``cpt_<code>``)."""
    base = f"{parent_base}.cpt_{code}"
    return Group(
        type="group",
        title=f"CPT {code}",
        codes=Codes(cpt=[code]),
        applicable_when=applicable_when,
        prompt=ask(f"Can you provide coverage details for CPT code {code}?"),
        fields=service_fields(
            base,
            f"Is CPT code {code} covered under this plan? Please answer Yes, No, or N/A.",
            flavor,
        ),
    )


def treatment_tail(gate: Condition) -> dict[str, FormField]:
    """Treatment-level cycle_limit + additional_notes, gated on the service being covered."""
    return {
        "cycle_limit": Leaf(
            type="text",
            title="Cycle Limit",
            role="ask",
            required=True,
            inapplicable_value="N/A",
            special_values=["No Limit"],
            applicable_when=gate,
            prompt=ask("What is the cycle limit for this service?"),
        ),
        "additional_notes": Leaf(
            type="text",
            title="Additional Notes",
            role="ask",
            ui=Ui(widget="textarea"),
            applicable_when=gate,
            prompt=ask("Are there any additional notes or limitations for this service?"),
            inapplicable_value="N/A",
        ),
    }


def treatment_group(
    section: str,
    key: str,
    title: str,
    icd10: str,
    cpt_codes: list[str],
    group_ask: str,
) -> Group:
    """An infertility treatment: per-CPT service items + cycle limit + notes."""
    base = f"sections.{section}.{key}"
    fields: dict[str, FormField] = {
        f"cpt_{code}": cpt_group(base, code, "treatment") for code in cpt_codes
    }
    covered = [eq(f"{base}.cpt_{code}.covered", "Yes") for code in cpt_codes]
    fields.update(treatment_tail(covered[0] if len(covered) == 1 else any_of(*covered)))
    return Group(
        type="group",
        title=title,
        applicable_when=ref("infertility_covered"),
        codes=Codes(icd10=[icd10]),
        prompt=ask(group_ask),
        fields=fields,
    )


def cost_pair(base: str) -> Alternatives:
    """Ask-less either/or: copay OR coinsurance satisfies the cost-share requirement."""
    return Alternatives(members=[f"{base}.copay", f"{base}.coinsurance"])


def money_leaf(
    title: str,
    ask_text: str,
    special_values: list[str] | None = None,
    applicable_when: Condition | None = None,
) -> Leaf:
    return Leaf(
        type="currency",
        title=title,
        role="ask",
        required=True,
        special_values=special_values,
        applicable_when=applicable_when,
        prompt=ask(ask_text),
    )


def money_triplet(
    base: str,
    title: str,
    asks: tuple[str, str, str],
    total_specials: list[str],
    applicable_when: Condition | None = None,
) -> Group:
    """A total / met_amount / remaining money group; met+remaining skipped when the
    total is one of its no-op special values (``$0``/``None``/``No Limit``…)."""
    met_gate = not_in(f"{base}.total", total_specials)
    return Group(
        type="group",
        title=title,
        applicable_when=applicable_when,
        fields={
            "total": money_leaf("Total", asks[0], special_values=total_specials),
            "met_amount": money_leaf("Met Amount", asks[1], applicable_when=met_gate),
            "remaining": money_leaf(
                "Remaining", asks[2], special_values=["Met"], applicable_when=met_gate
            ),
        },
    )


def enum_ask(
    title: str,
    ask_text: str,
    values: list[str],
    *,
    required: bool | RequiredWhen = True,
    default: str | None = None,
    applicable_when: Condition | None = None,
    hints: list[str] | None = None,
) -> Leaf:
    return Leaf(
        type="enum",
        title=title,
        role="ask",
        values=values,
        required=required,
        default=default,
        applicable_when=applicable_when,
        prompt=ask(ask_text, hints),
    )


def text_ask(
    title: str,
    ask_text: str,
    *,
    type_: Literal["text", "phone", "date", "integer"] = "text",
    required: bool | RequiredWhen = False,
    default: str | None = None,
    special_values: list[str] | None = None,
    applicable_when: Condition | None = None,
    description: str | None = None,
    hints: list[str] | None = None,
) -> Leaf:
    return Leaf(
        type=type_,
        title=title,
        role="ask",
        required=required,
        default=default,
        special_values=special_values,
        applicable_when=applicable_when,
        description=description,
        prompt=ask(ask_text, hints),
        validation=DATE_VALIDATION if type_ == "date" else None,
    )
