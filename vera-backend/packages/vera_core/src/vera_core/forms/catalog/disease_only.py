"""Disease-Only Insurance Verification (disease_only) form schema — the author source.

Compiles to ``data/form_schemas/disease_only_verification.json`` (DSL 2.1). A small
but deliberately construct-complete schema: it exercises every UI-facing DSL feature
(context/ui_only sections, system_fields, table layout with nested code groups and
group-level extras, gating, defaults, special values, validation, contradictions) so
the dynamic renderer can be validated against something other than the IBV form.
"""

from __future__ import annotations

from vera_core.forms.authoring import (
    DATE_VALIDATION,
    YES_NO,
    ask,
    cpt_group,
    enum_ask,
    eq,
    ref,
    service_fields,
    text_ask,
)
from vera_core.forms.dsl import (
    AllCondition,
    Codes,
    Contradiction,
    FieldPrompt,
    FormField,
    FormSchemaDoc,
    Group,
    Leaf,
    PromotedFields,
    Range,
    RequiredWhen,
    Section,
    SectionPrompt,
    Task,
    Ui,
    Validation,
)

_GROUP_PLAN = eq("sections.policy_details.plan_type", "Group")

_DISEASES: list[tuple[str, str, str, list[str], str]] = [
    # key, title, icd10, cpt codes, group ask
    (
        "cancer",
        "Cancer",
        "C80.1",
        ["77067", "85025"],
        "Can you provide coverage and benefit details for the cancer diagnosis benefit?",
    ),
    (
        "cardiac",
        "Cardiac Disease",
        "I25.10",
        ["93000"],
        "Can you provide coverage and benefit details for the cardiac disease benefit?",
    ),
    (
        "stroke",
        "Stroke",
        "I63.9",
        ["70450"],
        "Can you provide coverage and benefit details for the stroke benefit?",
    ),
]
_WAITING_PERIODS = ["None", "30 days", "90 days"]


def _context_sections() -> dict[str, Section]:
    return {
        "patient_information": Section(
            title="Patient Information",
            role="context",
            description="Patient identity supplied at intake; background for the agent.",
            fields={
                "chart_number": Leaf(
                    type="text",
                    title="Chart Number",
                    role="input",
                    default="N/A",
                    description="Clinic-internal chart number. Display only; never part of the call.",
                ),
                "patient_name": Leaf(
                    type="text", title="Patient Name", role="context", required=True
                ),
                "patient_dob": Leaf(
                    type="date",
                    title="Patient Date of Birth",
                    role="context",
                    required=True,
                    validation=DATE_VALIDATION,
                ),
                "patient_gender": Leaf(
                    type="enum",
                    title="Patient Gender",
                    role="context",
                    values=["Female", "Male", "Other"],
                    default="N/A",
                    required=True,
                ),
            },
        ),
        "appointment_information": Section(
            title="Appointment Information",
            role="context",
            description=(
                "Upcoming appointment details supplied at intake; background for the agent."
            ),
            fields={
                "appointment_type": Leaf(
                    type="enum",
                    title="Appointment Type",
                    role="context",
                    values=["New Patient", "Reverification", "Follow Up Visit", "N/A"],
                    default="N/A",
                    required=True,
                ),
                "appointment_date": Leaf(
                    type="date",
                    title="Appointment Date",
                    role="context",
                    required=True,
                    validation=DATE_VALIDATION,
                ),
            },
        ),
        "verification_information": Section(
            title="Verification Information",
            role="context",
            description="Who is running this verification and how the clinic can be called back.",
            fields={
                "verified_by": Leaf(
                    type="text",
                    title="Verified By",
                    role="context",
                    description="Human supervisor named in the call introduction.",
                ),
                "verified_at": Leaf(
                    type="date", title="Verified At", role="input", validation=DATE_VALIDATION
                ),
                "callback_number": Leaf(
                    type="phone",
                    title="Callback Number",
                    role="context",
                    default="N/A",
                    description="The callback phone number of your supervisor.",
                ),
            },
        ),
    }


def _policy_details() -> Section:
    return Section(
        title="Policy Details",
        fields={
            "policy_number": Leaf(
                type="text",
                title="Policy / Member ID",
                role="confirm",
                required=True,
                prompt=FieldPrompt(
                    confirm="I have the member ID as {{value}} — can you confirm that is correct?"
                ),
            ),
            "plan_name": text_ask(
                "Plan Name",
                "What is the name of this disease-only plan?",
                required=True,
                special_values=[
                    "Critical Illness Basic",
                    "Critical Illness Plus",
                    "Disease Shield Complete",
                ],
            ),
            "plan_type": Leaf(
                type="enum",
                title="Plan Type",
                role="ask",
                required=True,
                values=["Individual", "Group"],
                special_values=["Association"],
                prompt=ask("Is this an individual or a group disease-only policy?"),
            ),
            "effective_date": text_ask(
                "Effective Date",
                "What is the effective date for this coverage?",
                type_="date",
                required=True,
            ),
            "group_number": text_ask(
                "Group Number",
                "What is the group number for this policy?",
                required=RequiredWhen(when=_GROUP_PLAN),
                applicable_when=_GROUP_PLAN,
            ),
        },
    )


def _coverage_summary() -> Section:
    base = "sections.coverage_summary"
    return Section(
        title="Coverage Summary",
        fields={
            "disease_coverage_active": enum_ask(
                "Disease Coverage Active",
                "Is disease-specific coverage active on this policy?",
                YES_NO,
            ),
            "benefit_year_type": enum_ask(
                "Benefit Year Type",
                "Does the benefit year run on a Calendar Year or a Plan Year?",
                ["Calendar Year", "Plan Year"],
                default="Calendar Year",
            ),
            "renewal_date": Leaf(
                type="date",
                validation=DATE_VALIDATION,
                title="Renewal Date",
                role="ask",
                required=True,
                applicable_when=eq(f"{base}.benefit_year_type", "Plan Year"),
                inapplicable_value="N/A",
                prompt=ask("What is the renewal date for the plan year?"),
            ),
            "annual_benefit_maximum": Leaf(
                type="currency",
                title="Annual Benefit Maximum",
                role="ask",
                required=True,
                special_values=["No Limit"],
                validation=Validation(range=Range(min=0, max=1_000_000)),
                prompt=ask("What is the annual benefit maximum for disease coverage?"),
            ),
            "policy_state": Leaf(
                type="text",
                title="Policy State",
                role="ask",
                required=True,
                validation=Validation(pattern="^[A-Z]{2}$"),
                description="Two-letter state code whose law governs the policy.",
                prompt=ask("What is the two-letter policy state, for example T X for Texas?"),
            ),
            "state_mandate": enum_ask(
                "State Disease Coverage Mandate",
                "Is there a state mandate for disease-specific coverage on this policy?",
                YES_NO,
            ),
        },
    )


def _notes_leaf(benefit_label: str) -> Leaf:
    """Free-text notes/limitations textarea for a disease benefit (skip-filled 'N/A')."""
    return Leaf(
        type="text",
        title="Notes",
        role="ask",
        ui=Ui(widget="textarea"),
        inapplicable_value="N/A",
        prompt=ask(f"Are there any notes or limitations for the {benefit_label} benefit?"),
    )


def _covered_diseases() -> Section:
    base = "sections.covered_diseases"
    fields: dict[str, FormField] = {}
    for key, title, icd10, cpt_codes, group_ask in _DISEASES:
        disease_fields: dict[str, FormField] = {
            f"cpt_{code}": cpt_group(f"{base}.{key}", code, "treatment") for code in cpt_codes
        }
        disease_fields["waiting_period"] = text_ask(
            "Waiting Period",
            f"Is there a waiting period for the {title.lower()} benefit?",
            required=True,
            special_values=_WAITING_PERIODS,
        )
        disease_fields["notes"] = _notes_leaf(title.lower())
        fields[key] = Group(
            type="group",
            title=title,
            codes=Codes(icd10=[icd10]),
            prompt=ask(group_ask),
            fields=disease_fields,
        )
    # Leaf-only group: the service item leaves sit directly on the disease group.
    kf_base = f"{base}.kidney_failure"
    kf_fields: dict[str, FormField] = service_fields(
        kf_base,
        "Is the kidney failure benefit covered under this plan? Please answer Yes, No, or N/A.",
        "plain",
    )
    kf_fields["notes"] = _notes_leaf("kidney failure")
    fields["kidney_failure"] = Group(
        type="group",
        title="Kidney Failure",
        codes=Codes(icd10=["N18.6"]),
        prompt=ask("Can you provide coverage and benefit details for the kidney failure benefit?"),
        fields=kf_fields,
    )
    return Section(
        title="Covered Diseases",
        ui=Ui(layout="table"),
        applicable_when=ref("disease_covered"),
        prompt=SectionPrompt(
            intro="Now I'd like to go through the covered diseases one at a time."
        ),
        fields=fields,
    )


def _exclusions_limitations() -> Section:
    base = "sections.exclusions_limitations"
    has_exclusion = eq(f"{base}.pre_existing_exclusion", "Yes")
    return Section(
        title="Exclusions & Limitations",
        fields={
            "pre_existing_exclusion": enum_ask(
                "Pre-Existing Condition Exclusion",
                "Does this policy exclude pre-existing conditions?",
                YES_NO,
            ),
            "exclusion_lookback": text_ask(
                "Exclusion Lookback Period",
                "What is the lookback period for the pre-existing condition exclusion?",
                required=RequiredWhen(when=has_exclusion),
                special_values=["6 months", "12 months", "24 months"],
                applicable_when=has_exclusion,
            ),
        },
    )


def _insurance_reference() -> Section:
    return Section(
        title="Insurance Reference Information",
        description=("Reference details about the insurance provider, collected when available."),
        fields={
            "insurance_provider_name": text_ask(
                "Insurance Provider Name",
                "Could you provide the full name of the insurance provider?",
            ),
            "insurance_phone_number": text_ask(
                "Insurance Provider Phone",
                "What is the primary phone number for the insurance provider?",
                type_="phone",
            ),
        },
    )


def _wrap_up_sections() -> dict[str, Section]:
    return {
        "representative_details": Section(
            title="Insurance Representative",
            fields={
                "rep_name": text_ask(
                    "Representative Name",
                    "May I have your first name and last name initial?",
                    required=True,
                ),
                "call_reference_number": text_ask(
                    "Call Reference Number",
                    "May I have a call reference number for this call?",
                    required=True,
                ),
            },
        ),
        "form_information": Section(
            title="Additional Form Information",
            role="ui_only",
            fields={
                "practice": Leaf(type="text", title="Practice", role="input"),
                "form_type": Leaf(type="text", title="Form Type", role="input"),
            },
        ),
    }


def build_disease_only() -> FormSchemaDoc:
    return FormSchemaDoc(
        dsl_version="2.1",
        name="Disease-Only Insurance Verification",
        insurance_type="disease_only",
        description=(
            "Dummy disease-only (critical illness) benefits verification form. Small but "
            "construct-complete: exercises every DSL feature the dynamic renderer supports."
        ),
        system_fields={
            "chart_number": "sections.patient_information.chart_number",
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "patient_gender": "sections.patient_information.patient_gender",
            "member_id": "sections.policy_details.policy_number",
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "insurance_provider_name": (
                "sections.insurance_reference_information.insurance_provider_name"
            ),
            "insurance_provider_phone_number": (
                "sections.insurance_reference_information.insurance_phone_number"
            ),
            "verified_by": "sections.verification_information.verified_by",
            "callback_number": "sections.verification_information.callback_number",
            "form_completed_at": "sections.verification_information.verified_at",
        },
        promoted_fields=PromotedFields(
            patient_name="sections.patient_information.patient_name",
            patient_dob="sections.patient_information.patient_dob",
            chart_number="sections.patient_information.chart_number",
            appointment_date="sections.appointment_information.appointment_date",
            appointment_type="sections.appointment_information.appointment_type",
            member_id="sections.policy_details.policy_number",
            insurance_provider="sections.insurance_reference_information.insurance_provider_name",
            insurance_provider_phone_number=(
                "sections.insurance_reference_information.insurance_phone_number"
            ),
        ),
        rep_call_reference_number_field="sections.representative_details.call_reference_number",
        shared_conditions={
            "disease_covered": eq("sections.coverage_summary.disease_coverage_active", "Yes"),
        },
        sections={
            **_context_sections(),
            "policy_details": _policy_details(),
            "coverage_summary": _coverage_summary(),
            "covered_diseases": _covered_diseases(),
            "exclusions_limitations": _exclusions_limitations(),
            "insurance_reference_information": _insurance_reference(),
            **_wrap_up_sections(),
        },
        tasks=[
            Task(
                task_key="policy_basics",
                title="Policy Basics",
                prompt=(
                    "Verify the disease-only policy identity and overall coverage status "
                    "before anything else; the rest of the call depends on these answers."
                ),
                outro="Perfect, that covers the policy basics. One moment please.",
                sections=["policy_details", "coverage_summary"],
            ),
            Task(
                task_key="disease_coverage",
                title="Disease Coverage",
                prompt=(
                    "Go disease by disease. Establish coverage first, then benefits per "
                    "covered condition; skip sub-questions for conditions that are not "
                    "covered."
                ),
                intro="Now I'd like to verify the covered disease benefits.",
                outro="Thank you, that covers the disease benefits. Just a moment.",
                sections=["covered_diseases"],
            ),
            Task(
                task_key="limitations",
                title="Exclusions & Limitations",
                prompt=(
                    "Capture exclusions and lookback limitations verbatim - these drive "
                    "claim denials, so do not paraphrase."
                ),
                intro="Just a few questions about exclusions and limitations.",
                sections=["exclusions_limitations"],
            ),
            Task(
                task_key="wrap_up",
                title="Wrap Up",
                prompt=(
                    "Always run last: capture the representative's name and a call "
                    "reference number before ending the call politely."
                ),
                sections=["insurance_reference_information", "representative_details"],
            ),
        ],
        contradictions=[
            Contradiction(
                rule_key="mandate_requires_disease_coverage",
                when=AllCondition(
                    all=[
                        eq("sections.coverage_summary.state_mandate", "Yes"),
                        eq("sections.coverage_summary.disease_coverage_active", "No"),
                    ]
                ),
                fields=[
                    "sections.coverage_summary.disease_coverage_active",
                    "sections.coverage_summary.state_mandate",
                ],
                reason=(
                    "If the state mandates disease-specific benefits, disease coverage "
                    "must be active on the policy."
                ),
                clarify=(
                    "Earlier you mentioned there is a state mandate for disease coverage, but "
                    "disease-specific coverage is showing as not active — with a mandate, the "
                    "coverage should be active. Could you double-check whether disease-specific "
                    "coverage is active on this policy?"
                ),
            ),
        ],
    )
