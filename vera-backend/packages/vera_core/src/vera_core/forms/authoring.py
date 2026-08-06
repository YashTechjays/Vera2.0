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
    AskGroup,
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


def coverage_ask(service: str) -> str:
    """The standard service-level coverage question."""
    return f"Can you provide coverage and benefit details for {service}?"


def cost_ask(referent: str) -> str:
    """The single cost question. Copay and coinsurance are alternatives — one answer
    satisfies the pair — so they are asked together, not as two questions."""
    return f"What is the copay or coinsurance for {referent}?"


def service_asks(referent: str) -> tuple[str, str, str]:
    """The copay / coinsurance / prior-auth questions naming ``referent``.

    One canonical shape each: the compiler emits at most one of these per service, so there
    is no run of near-identical sentences left for a rotation to break up.
    """
    return (
        f"What is the copay amount for {referent}?",
        f"What is the coinsurance percentage for {referent}?",
        f"Is prior authorization required for {referent}?",
    )


Flavor = Literal["treatment", "male", "plain"]
_INAPPLICABLE: dict[Flavor, dict[str, str]] = {
    "treatment": {"covered": "No", "copay": "$0", "coinsurance": "0%", "prior_auth": "N/A"},
    "male": {"covered": "N/A", "copay": "N/A", "coinsurance": "N/A", "prior_auth": "N/A"},
    "plain": {"copay": "$0", "coinsurance": "0%", "prior_auth": "N/A"},
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
    *,
    referent: str,
) -> dict[str, FormField]:
    """The standard covered/copay/coinsurance/prior_auth service item.

    ``base`` is the root-anchored path of the containing group; sub-field gates are
    wired to ``{base}.covered``. ``flavor`` picks the skip-fill defaults. ``referent``
    is how the copay/coinsurance/prior-auth questions name their subject aloud (a CPT
    code, or the service itself where there is no code).

    ``"plain"`` flavor omits a ``covered`` inapplicable default because plain sections
    may have no ancestor ``applicable_when`` gate — a DSL invariant enforced by
    ``dsl.py``'s document validator. The copay/coinsurance/prior_auth sub-fields are
    always self-gated (``applicable_when=gate``) and therefore carry their defaults safely.
    """
    inapplicable = _INAPPLICABLE[flavor]
    gate = eq(f"{base}.covered", "Yes")
    copay_ask, coinsurance_ask, prior_auth_ask = service_asks(referent)
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
            prompt=ask(copay_ask),
        ),
        "coinsurance": Leaf(
            type="percent",
            title="Coinsurance (%)",
            role="ask",
            required=True,
            inapplicable_value=inapplicable.get("coinsurance"),
            validation=Validation(range=Range(min=0, max=100)),
            applicable_when=gate,
            prompt=ask(coinsurance_ask),
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
            prompt=ask(prior_auth_ask),
        ),
    }


def cpt_group(
    parent_base: str,
    code: str,
    flavor: Flavor,
    *,
    service: str,
    applicable_when: Condition | None = None,
) -> Group:
    """A per-CPT-code service item under ``parent_base`` (group key = ``cpt_<code>``).

    ``service`` is the spoken name of the service the code belongs to, said alongside the
    code. A multi-code service asks these as ONE panel question per sub-field (see
    `panel_ask_groups`); the per-code text below is what a single-code service says.
    """
    base = f"{parent_base}.cpt_{code}"
    return Group(
        type="group",
        title=f"CPT {code}",
        codes=Codes(cpt=[code]),
        applicable_when=applicable_when,
        # Documents the group. The panel heading the compiler emits carries the code, so this
        # is not the spoken question — the leaf asks below (or the panel ask group) are.
        prompt=ask(f"Can you provide coverage details for CPT code {code}?"),
        fields=service_fields(
            base,
            f"Is CPT code {code} for {service} covered under this plan?",
            flavor,
            referent=f"CPT code {code} under {service}",
        ),
    )


def cpt_groups(
    parent_base: str,
    codes: list[str],
    flavor: Flavor,
    *,
    service: str,
    applicable_when: Condition | None = None,
) -> dict[str, FormField]:
    """One `cpt_group` per code."""
    return {
        f"cpt_{code}": cpt_group(
            parent_base, code, flavor, service=service, applicable_when=applicable_when
        )
        for code in codes
    }


_PANEL_ASKS = {
    "covered": "Are {title} codes {codes} covered under this plan?",
    "copay": None,  # supplied by the cost_pair alternative, which merges it with coinsurance
    "coinsurance": None,
    "prior_auth": "Is prior authorization required for {title}?",
}


def panel_ask_groups(base: str, title: str, codes: list[str]) -> list[AskGroup]:
    """One spoken question per sub-field, fanned out over a service's CPT codes.

    A rep quotes benefits per service, not per code — reading eight codes back one at a time
    is what made these tasks long. Below two codes there is nothing to merge (an `AskGroup`
    needs >= 2 members), so a single-code service keeps its per-code wording.
    """
    if len(codes) < 2:
        return []
    listed = ", ".join(codes)
    return [
        AskGroup(
            fields=[f"{base}.cpt_{code}.{sub}" for code in codes],
            ask=template.format(title=title, codes=listed),
        )
        for sub, template in _PANEL_ASKS.items()
        if template is not None
    ]


def panel_cost_pairs(base: str, title: str, codes: list[str]) -> list[Alternatives]:
    """The cost question for a service, fanned out over its codes.

    Copay and coinsurance are an either/or, so they are ONE question with two acceptable
    answers rather than two questions plus a footnote nobody can act on.
    """
    if len(codes) < 2:
        return [cost_pair(f"{base}.cpt_{code}") for code in codes]
    return [
        Alternatives(
            members=[
                *(f"{base}.cpt_{code}.copay" for code in codes),
                *(f"{base}.cpt_{code}.coinsurance" for code in codes),
            ],
            ask=cost_ask(title),
        )
    ]


def treatment_tail(gate: Condition, *, service: str) -> dict[str, FormField]:
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
            prompt=ask(f"What is the cycle limit for {service}?"),
        ),
        "additional_notes": Leaf(
            type="text",
            title="Additional Notes",
            role="ask",
            ui=Ui(widget="textarea"),
            applicable_when=gate,
            prompt=ask(f"Are there any additional notes or limitations for {service}?"),
            inapplicable_value="N/A",
        ),
    }


def treatment_group(
    section: str,
    key: str,
    title: str,
    icd10: str | None,
    cpt_codes: list[str],
    group_ask: str,
    *,
    service: str,
) -> Group:
    """An infertility treatment: per-CPT service items + cycle limit + notes."""
    base = f"sections.{section}.{key}"
    fields: dict[str, FormField] = cpt_groups(base, cpt_codes, "treatment", service=service)
    covered = [eq(f"{base}.cpt_{code}.covered", "Yes") for code in cpt_codes]
    fields.update(
        treatment_tail(covered[0] if len(covered) == 1 else any_of(*covered), service=service)
    )
    return Group(
        type="group",
        title=title,
        applicable_when=ref("infertility_covered"),
        codes=Codes(icd10=[icd10] if icd10 else None),
        prompt=ask(group_ask),
        fields=fields,
    )


def cost_pair(base: str, referent: str = "this service") -> Alternatives:
    """Either/or: copay OR coinsurance satisfies the cost-share requirement, so the pair is
    one spoken question with two acceptable answers."""
    return Alternatives(members=[f"{base}.copay", f"{base}.coinsurance"], ask=cost_ask(referent))


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
