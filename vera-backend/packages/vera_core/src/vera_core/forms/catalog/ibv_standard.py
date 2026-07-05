"""IBV Standard (infertility_treatment) form schema — the author source.

Compiles to ``data/form_schemas/ibv_form_standard_v2.json`` (DSL 2.1). Business
content — questions, gates, codes, contradictions — lives here as typed Python;
the compiled JSON is the runtime artifact.
"""

from __future__ import annotations

from vera_core.forms.authoring import (
    YES_NO,
    YES_NO_NA,
    ask,
    cost_pair,
    cpt_group,
    enum_ask,
    eq,
    money_triplet,
    ref,
    service_fields,
    text_ask,
    treatment_group,
    treatment_tail,
)
from vera_core.forms.dsl import (
    AllCondition,
    Alternatives,
    AnyCondition,
    AskGroup,
    Codes,
    Comparison,
    Contradiction,
    Derive,
    FieldPrompt,
    FlowRule,
    FormField,
    FormSchemaDoc,
    Group,
    Leaf,
    RequiredWhen,
    Section,
    Task,
    Ui,
    Validation,
)

_FAMILY = ref("family_coverage")
_REQUIRED_WHEN_FAMILY = RequiredWhen(when=_FAMILY)

_DEDUCTIBLE_NOOPS = ["$0", "None", "No Deductible", "Unlimited", "No Limit"]
_OOP_NOOPS = ["$0", "None", "Unlimited", "No Limit"]
_NO_LIMIT = ["No Limit", "Unlimited"]

_TREATMENTS: list[tuple[str, str, str, list[str], str]] = [
    # key, title, icd10, cpt codes, group ask
    (
        "intrauterine_insemination",
        "Intrauterine Insemination (IUI)",
        "Z31.89",
        ["58323", "58322", "89261"],
        "Can you provide coverage and benefit details for intrauterine insemination, or IUI?",
    ),
    (
        "in_vitro_fertilization",
        "In Vitro Fertilization (IVF)",
        "Z31.83",
        ["58970", "89280", "89253"],
        "Can you provide coverage and benefit details for in vitro fertilization, or IVF?",
    ),
    (
        "egg_cryopreservation_elective",
        "Egg Cryopreservation (Elective)",
        "Z31.83",
        ["89337"],
        "Can you provide coverage and benefit details for elective egg cryopreservation?",
    ),
    (
        "embryo_cryopreservation",
        "Embryo Cryopreservation",
        "Z31.83",
        ["89258", "89342"],
        "Can you provide coverage and benefit details for embryo cryopreservation?",
    ),
    (
        "frozen_embryo_transfer",
        "Frozen Embryo Transfer (FET)",
        "Z31.83",
        ["58974"],
        "Can you provide coverage and benefit details for frozen embryo transfer, or FET?",
    ),
    (
        "egg_cryopreservation_cancer",
        "Egg Cryopreservation (Cancer-Related)",
        "Z31.83",
        ["89337"],
        "Can you provide coverage and benefit details for egg cryopreservation related to cancer treatment?",
    ),
    (
        "embryo_biopsy",
        "Embryo Biopsy",
        "Z31.83",
        ["89290", "89291"],
        "Can you provide coverage and benefit details for embryo biopsy?",
    ),
]
_DIAG_CODES = ["58340", "82670", "83001", "83002", "84146", "84443", "84144", "76830"]
_GENERAL: list[tuple[str, str, str, str]] = [
    # key, title, cpt code, group ask
    (
        "office_visits",
        "Office Visits",
        "99211",
        "Can you provide coverage and benefit details for office visits?",
    ),
    (
        "asc_professional",
        "ASC Professional Services",
        "58555",
        "Can you provide coverage and benefit details for ambulatory surgical center professional services?",
    ),
    (
        "asc_facility",
        "ASC Facility",
        "58555",
        "Can you provide coverage and benefit details for the ambulatory surgical center facility?",
    ),
]
_MALE: list[tuple[str, str, str, str]] = [
    (
        "semen_analysis",
        "Semen Analysis",
        "89320",
        "Can you provide coverage and benefit details for semen analysis?",
    ),
    (
        "sperm_cryopreservation",
        "Sperm Cryopreservation",
        "89259",
        "Can you provide coverage and benefit details for sperm cryopreservation?",
    ),
]


def _context_sections() -> dict[str, Section]:
    return {
        "patient_information": Section(
            title="Patient Information",
            role="context",
            description=(
                "Patient and spouse identity supplied at intake. Provided to the agent as "
                "background; spouse identity is confirmed with the representative only for "
                "family policies."
            ),
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
                    type="date", title="Patient Date of Birth", role="context", required=True
                ),
                "patient_gender": Leaf(
                    type="enum",
                    title="Patient Gender",
                    role="context",
                    values=["Female", "Male", "Other"],
                    default="N/A",
                    required=True,
                ),
                "spouse_partner_name": Leaf(
                    type="text",
                    title="Spouse / Partner Name",
                    role="confirm",
                    default="N/A",
                    confirm_in_task="insurance_basics",
                    applicable_when=_FAMILY,
                    required=_REQUIRED_WHEN_FAMILY,
                    prompt=FieldPrompt(
                        confirm=(
                            "Can we also check the spouse on the plan? I have the spouse listed "
                            "as {{value}} — can you confirm that is correct?"
                        )
                    ),
                ),
                "spouse_partner_dob": Leaf(
                    type="date",
                    title="Spouse / Partner Date of Birth",
                    role="confirm",
                    default="N/A",
                    confirm_in_task="insurance_basics",
                    applicable_when=_FAMILY,
                    required=_REQUIRED_WHEN_FAMILY,
                    prompt=FieldPrompt(
                        confirm="And the spouse's date of birth I have is {{value}} — is that right?"
                    ),
                ),
                "spouse_gender": Leaf(
                    type="enum",
                    title="Spouse Gender",
                    role="context",
                    values=["Female", "Male", "Other"],
                    default="N/A",
                    required=_REQUIRED_WHEN_FAMILY,
                ),
            },
        ),
        "appointment_information": Section(
            title="Appointment Information",
            role="context",
            description=(
                "Upcoming appointment details. The agent answers date-of-service questions "
                "from these values."
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
                    type="date", title="Appointment Date", role="context", required=True
                ),
            },
        ),
        "verification_information": Section(
            title="Verification Information",
            role="context",
            description=(
                "Who is running/supervising this verification and how the clinic can be "
                "called back."
            ),
            fields={
                "verified_by": Leaf(
                    type="text",
                    title="Verified By",
                    role="context",
                    description="Human supervisor named in the call introduction.",
                ),
                "verified_at": Leaf(type="date", title="Verified At", role="input"),
                "callback_number": Leaf(
                    type="phone",
                    title="Callback Number",
                    role="context",
                    default="N/A",
                    description="The callback phone number of your supervisor",
                ),
            },
        ),
        "hospital_information": Section(
            title="Hospital Information",
            role="context",
            description="Facility identifiers the agent provides when the IVR or representative asks.",
            fields={
                "hospital_name": Leaf(
                    type="text", title="Hospital Name", role="context", required=True
                ),
                "hospital_address": Leaf(type="text", title="Hospital Address", role="context"),
                "tax_id": Leaf(
                    type="text",
                    title="Tax ID",
                    role="context",
                    required=True,
                    validation=Validation(pattern="^[0-9]{9}$"),
                ),
                "npi": Leaf(
                    type="text",
                    title="Facility NPI",
                    role="context",
                    required=True,
                    validation=Validation(pattern="^[0-9]{10}$"),
                ),
            },
        ),
        "provider_reference_information": Section(
            title="Provider Reference Information",
            role="context",
            description="Ordering provider identifiers the agent provides when asked.",
            fields={
                "provider_name": Leaf(
                    type="text", title="Provider Name", role="context", required=True
                ),
                "npi": Leaf(
                    type="text",
                    title="Provider NPI",
                    role="context",
                    validation=Validation(pattern="^[0-9]{10}$"),
                ),
                "office_location": Leaf(type="text", title="Office Location", role="context"),
            },
        ),
    }


def _insurance_information() -> Section:
    base = "sections.insurance_information"
    return Section(
        title="Insurance Information",
        ask_groups=[
            AskGroup(
                fields=[f"{base}.plan_type", f"{base}.cob_status"],
                ask=(
                    "What type of plan is this — PPO, HMO, EPO, or something else? And for "
                    "coordination of benefits, is this plan primary, secondary, or tertiary?"
                ),
            ),
            AskGroup(
                fields=[f"{base}.group_name", f"{base}.group_number"],
                ask="What is the group name and group number?",
            ),
        ],
        fields={
            "doctor_inside_network": enum_ask(
                "Doctor Inside Network", "Is the doctor inside the insurance network?", YES_NO
            ),
            "facility_inside_network": enum_ask(
                "Facility Inside Network", "Is the facility inside the insurance network?", YES_NO
            ),
            "out_of_network_coverage": enum_ask(
                "Out-of-Network Coverage",
                "Does this plan cover out-of-network benefits?",
                YES_NO,
                applicable_when=AllCondition(
                    all=[
                        eq(f"{base}.doctor_inside_network", "No"),
                        eq(f"{base}.facility_inside_network", "No"),
                    ]
                ),
            ),
            "plan_type": text_ask(
                "Plan Type",
                "What type of plan is this — PPO, HMO, EPO, or something else?",
                required=True,
                special_values=["PPO", "HMO", "EPO", "POS"],
            ),
            "cob_status": enum_ask(
                "Coordination of Benefits",
                "For coordination of benefits, is this plan primary, secondary, or tertiary?",
                ["Primary", "Secondary", "Tertiary"],
            ),
            "policy_number": Leaf(
                type="text",
                title="Policy / Member ID",
                role="confirm",
                required=True,
                prompt=FieldPrompt(
                    confirm="I have the member ID as {{value}} — can you confirm that is correct?"
                ),
            ),
            "group_name": text_ask(
                "Group Name", "What is the group name?", required=True, default="N/A"
            ),
            "group_number": text_ask(
                "Group Number", "What is the group number?", required=True, default="N/A"
            ),
            "policy_situs": text_ask(
                "Policy Situs",
                "What is the policy state or contract state?",
                required=True,
                default="N/A",
                description="The state whose law governs the policy (contract state).",
            ),
        },
    )


def _benefit_coverage() -> Section:
    return Section(
        title="Benefit Coverage",
        fields={
            "benefit_year_type": enum_ask(
                "Benefit Year Type",
                "Does the benefit year run on a Calendar Year or a Plan Year?",
                ["Calendar Year", "Plan Year"],
            ),
            "renewal_date": text_ask(
                "Renewal Date",
                "What is the renewal date for the plan year?",
                type_="date",
                required=True,
                applicable_when=eq("sections.benefit_coverage.benefit_year_type", "Plan Year"),
            ),
            "plan_effective_date": Leaf(
                type="date",
                title="Plan Effective Date",
                role="ask",
                required=True,
                derive=Derive(
                    when=eq("sections.benefit_coverage.benefit_year_type", "Calendar Year"),
                    value="01/01/{{current_year}}",
                ),
                prompt=ask("What is the effective date for this coverage?"),
            ),
            "coverage_type": enum_ask(
                "Coverage Type",
                "Is this an individual or a family policy? Please answer Individual or Family.",
                ["Individual", "Family"],
            ),
            "pcp_referral_required": enum_ask(
                "PCP Referral Required",
                "Does this plan require a PCP referral?",
                YES_NO,
                default="N/A",
                applicable_when=eq("sections.insurance_information.plan_type", "HMO"),
            ),
            "telehealth_covered": enum_ask(
                "Telehealth Covered",
                "Does this plan cover telehealth services?",
                ["Yes", "No", "Limited"],
                default="N/A",
                hints=[
                    "If the representative asks whether we use our own telehealth platform, "
                    'answer exactly "Yes", then re-ask this question.'
                ],
            ),
            "plan_fund_type": enum_ask(
                "Plan Fund Type",
                "Is this plan self insured or fully funded?",
                ["Self Insured", "Fully Funded"],
            ),
            "employer_support_size": enum_ask(
                "Employer Support Size",
                "Is the employer group supporting this plan a small group or a large group?",
                ["Small Group", "Large Group"],
            ),
            "infertility_plan_mandate": enum_ask(
                "Infertility Plan Mandate",
                "Is there an infertility plan mandate on this policy?",
                YES_NO,
                hints=["Pronounce as: in-fer-TIL-ih-tee plan MAN-date."],
            ),
        },
    )


def _infertility_treatment() -> Section:
    oi_base = "sections.infertility_treatment.ovulation_induction"
    oi_fields = service_fields(
        oi_base,
        "Is ovulation induction covered under this plan? Please answer Yes, No, or N/A.",
        "treatment",
    )
    oi_fields.update(treatment_tail(eq(f"{oi_base}.covered", "Yes")))

    fields: dict[str, FormField] = {
        "infertility_tx_covered": enum_ask(
            "Infertility Treatment Covered",
            "Is infertility treatment covered under this plan?",
            YES_NO,
        )
    }
    for key, title, icd10, codes, group_ask in _TREATMENTS[:1]:
        fields[key] = treatment_group("infertility_treatment", key, title, icd10, codes, group_ask)
    fields["ovulation_induction"] = Group(
        type="group",
        title="Ovulation Induction",
        applicable_when=ref("infertility_covered"),
        codes=Codes(icd10=["Z31.89"]),
        prompt=ask("Can you provide coverage and benefit details for ovulation induction?"),
        fields=oi_fields,
    )
    for key, title, icd10, codes, group_ask in _TREATMENTS[1:]:
        fields[key] = treatment_group("infertility_treatment", key, title, icd10, codes, group_ask)

    alternatives = [
        Alternatives(
            members=[
                "sections.infertility_treatment.egg_cryopreservation_elective",
                "sections.infertility_treatment.egg_cryopreservation_cancer",
            ],
            ask=(
                "Can you provide coverage and benefit details for egg cryopreservation — is it "
                "covered as elective, or for cancer-related fertility preservation?"
            ),
        ),
        cost_pair(oi_base),
    ]
    for key, _title, _icd10, codes, _ask_text in _TREATMENTS:
        alternatives.extend(
            cost_pair(f"sections.infertility_treatment.{key}.cpt_{code}") for code in codes
        )
    return Section(
        title="Infertility Treatment",
        ui=Ui(layout="table"),
        alternatives=alternatives,
        fields=fields,
    )


def _diagnostic_testing() -> Section:
    base = "sections.diagnostic_testing"
    panel_asks = {
        "covered": (
            "Are diagnostic labs, X-ray and ultrasound services covered under this plan? "
            "Please answer Yes, No, or N/A."
        ),
        "copay": "What is the copay amount for diagnostic testing?",
        "coinsurance": "What is the coinsurance percentage for diagnostic testing?",
        "prior_auth": "Is prior authorization required for diagnostic testing? Please answer Yes, No, or N/A.",
    }
    return Section(
        title="Diagnostic Testing (Labs, X-ray & Ultrasound)",
        ui=Ui(layout="table"),
        codes=Codes(icd10=["Z31.41"], speak_cpt=True),
        ask_groups=[
            AskGroup(fields=[f"{base}.cpt_{c}.{sub}" for c in _DIAG_CODES], ask=panel_ask)
            for sub, panel_ask in panel_asks.items()
        ],
        alternatives=[cost_pair(f"{base}.cpt_{c}") for c in _DIAG_CODES],
        fields={f"cpt_{c}": cpt_group(base, c, "plain") for c in _DIAG_CODES},
    )


def _general_coverage() -> Section:
    base = "sections.general_coverage"
    return Section(
        title="General Coverage",
        ui=Ui(layout="table"),
        alternatives=[
            Alternatives(
                members=[f"{base}.asc_professional", f"{base}.asc_facility"],
                ask=(
                    "Can you provide coverage and benefit details for ambulatory surgical "
                    "center services — is that billed as professional or facility?"
                ),
            ),
            *(cost_pair(f"{base}.{key}.cpt_{code}") for key, _t, code, _a in _GENERAL),
        ],
        fields={
            key: Group(
                type="group",
                title=title,
                codes=Codes(icd10=["Z31.41"]),
                prompt=ask(group_ask),
                fields={f"cpt_{code}": cpt_group(f"{base}.{key}", code, "plain")},
            )
            for key, title, code, group_ask in _GENERAL
        },
    )


def _financial_sections() -> dict[str, Section]:
    ltm = "sections.lifetime_maximum"
    ltm_gate = Comparison(field=f"{ltm}.total", op="not_in", value=_NO_LIMIT)
    return {
        "deductibles": Section(
            title="Deductibles",
            fields={
                "individual": money_triplet(
                    "sections.deductibles.individual",
                    "Individual Deductible",
                    (
                        "What is the total individual deductible?",
                        "How much of the individual deductible has been met?",
                        "What is the remaining individual deductible?",
                    ),
                    _DEDUCTIBLE_NOOPS,
                ),
                "family": money_triplet(
                    "sections.deductibles.family",
                    "Family Deductible",
                    (
                        "What is the total family deductible?",
                        "How much of the family deductible has been met?",
                        "What is the remaining family deductible?",
                    ),
                    _DEDUCTIBLE_NOOPS,
                    applicable_when=_FAMILY,
                ),
            },
        ),
        "out_of_pocket": Section(
            title="Out-of-Pocket Maximum",
            fields={
                "individual": money_triplet(
                    "sections.out_of_pocket.individual",
                    "Individual Out-of-Pocket",
                    (
                        "What is the total out-of-pocket limit for an individual?",
                        "How much of the individual out-of-pocket has been met?",
                        "What is the remaining out-of-pocket for an individual?",
                    ),
                    _OOP_NOOPS,
                ),
                "family": money_triplet(
                    "sections.out_of_pocket.family",
                    "Family Out-of-Pocket",
                    (
                        "What is the total out-of-pocket limit for family coverage?",
                        "How much of the family out-of-pocket has been met?",
                        "What is the remaining out-of-pocket for a family?",
                    ),
                    _OOP_NOOPS,
                    applicable_when=_FAMILY,
                ),
            },
        ),
        "lifetime_maximum": Section(
            title="Infertility Lifetime Maximum",
            fields={
                "total": Leaf(
                    type="currency",
                    title="Lifetime Maximum",
                    role="ask",
                    required=True,
                    special_values=_NO_LIMIT,
                    prompt=ask("What is the total lifetime maximum for infertility services?"),
                ),
                "met_amount": Leaf(
                    type="currency",
                    title="Met Amount",
                    role="ask",
                    required=True,
                    applicable_when=ltm_gate,
                    prompt=ask("How much of the lifetime maximum has been used?"),
                ),
                "remaining": Leaf(
                    type="currency",
                    title="Remaining",
                    role="ask",
                    required=True,
                    applicable_when=ltm_gate,
                    prompt=ask("What is the remaining lifetime maximum amount?"),
                ),
                "applicable_area": enum_ask(
                    "Applicable Area",
                    (
                        "What areas does the lifetime maximum apply to — infertility treatment, "
                        "infertility treatment and medication, diagnostic and infertility "
                        "treatment, or diagnostic only?"
                    ),
                    [
                        "Infertility Treatment",
                        "Infertility Treatment and Medication",
                        "Diagnostic and Infertility Treatment",
                        "Diagnostic",
                    ],
                    applicable_when=ltm_gate,
                ),
                "additional_notes": Leaf(
                    type="text",
                    title="Additional Notes",
                    role="ask",
                    ui=Ui(widget="textarea"),
                    prompt=ask(
                        "Are there any additional notes regarding the lifetime maximum coverage?"
                    ),
                ),
                "lifetime_cycle_max": text_ask(
                    "Lifetime Cycle Maximum",
                    "What is the infertility lifetime cycle maximum for this patient?",
                    type_="integer",
                    required=True,
                    special_values=["No Limit"],
                ),
                "cycles_used": text_ask(
                    "Cycles Used to Date",
                    "How many treatment cycles have been used to date?",
                    type_="integer",
                    required=True,
                ),
            },
        ),
        "embryo_cryo_storage": Section(
            title="Embryo Cryo Storage",
            applicable_when=eq(
                "sections.infertility_treatment.embryo_cryopreservation.cpt_89342.covered", "Yes"
            ),
            codes=Codes(cpt=["89342"]),
            fields={
                "storage_time_coverage": text_ask(
                    "Storage Time Coverage",
                    "Is embryo cryopreservation storage time covered, and for how long?",
                    required=True,
                )
            },
        ),
    }


def _male_partner_coverage() -> Section:
    base = "sections.male_partner_coverage"
    return Section(
        title="Male Partner Coverage",
        ui=Ui(layout="table"),
        applicable_when=ref("male_partner_in_scope"),
        alternatives=[cost_pair(f"{base}.{key}.cpt_{code}") for key, _t, code, _a in _MALE],
        fields={
            "male_partner_covered": enum_ask(
                "Male Partner Services Covered",
                "Are male partner fertility services covered under this plan?",
                YES_NO,
            ),
            **{
                key: Group(
                    type="group",
                    title=title,
                    codes=Codes(icd10=["Z31.84"]),
                    applicable_when=eq(f"{base}.male_partner_covered", "Yes"),
                    prompt=ask(group_ask),
                    fields={f"cpt_{code}": cpt_group(f"{base}.{key}", code, "male")},
                )
                for key, title, code, group_ask in _MALE
            },
        },
    )


def _admin_sections() -> dict[str, Section]:
    return {
        "enrollment": Section(
            title="Enrollment",
            ask_groups=[
                AskGroup(
                    fields=[
                        "sections.enrollment.enrollment_provider_name",
                        "sections.enrollment.enrollment_provider_phone",
                    ],
                    ask="What is the provider name and phone number for enrollment?",
                )
            ],
            fields={
                "enrollment_required": enum_ask(
                    "Enrollment Required",
                    "Is enrollment required for this patient? Please answer Yes, No, or N/A.",
                    YES_NO_NA,
                    default="N/A",
                ),
                "enrollment_provider_name": text_ask(
                    "Enrollment Provider Name",
                    "What is the provider name for enrollment?",
                    required=True,
                    applicable_when=eq("sections.enrollment.enrollment_required", "Yes"),
                ),
                "enrollment_provider_phone": text_ask(
                    "Enrollment Provider Phone",
                    "What is the phone number for the enrollment provider?",
                    type_="phone",
                    required=True,
                    applicable_when=eq("sections.enrollment.enrollment_required", "Yes"),
                ),
            },
        ),
        "authorization_department": Section(
            title="Authorization Department",
            ask_groups=[
                AskGroup(
                    fields=[
                        "sections.authorization_department.auth_department_name",
                        "sections.authorization_department.auth_department_phone",
                    ],
                    ask="What is the authorization department name and phone number?",
                )
            ],
            fields={
                "auth_department_name": text_ask(
                    "Authorization Department Name",
                    "What is the authorization department name?",
                    required=True,
                    applicable_when=ref("any_service_requires_prior_auth"),
                ),
                "auth_department_phone": text_ask(
                    "Authorization Department Phone",
                    "What is the authorization department phone number?",
                    type_="phone",
                    required=True,
                    applicable_when=ref("any_service_requires_prior_auth"),
                ),
                "center_of_excellence_required": enum_ask(
                    "Center of Excellence Required",
                    "Is a center of excellence required for infertility treatment? Please answer Yes, No, or N/A.",
                    YES_NO_NA,
                ),
            },
        ),
        "third_party_administrator": Section(
            title="Third Party Administrator",
            ask_groups=[
                AskGroup(
                    fields=[
                        "sections.third_party_administrator.tpa_name",
                        "sections.third_party_administrator.tpa_member_id",
                    ],
                    ask=(
                        "What is the name of the third party administrator, and could you also "
                        "provide this patient's member ID with the TPA?"
                    ),
                )
            ],
            fields={
                "tpa_exists": enum_ask(
                    "TPA Exists",
                    "Is there a third party administrator for infertility services?",
                    YES_NO,
                ),
                "tpa_name": text_ask(
                    "TPA Name",
                    "What is the name of the third party administrator?",
                    required=True,
                    applicable_when=eq("sections.third_party_administrator.tpa_exists", "Yes"),
                ),
                "tpa_member_id": text_ask(
                    "TPA Member ID",
                    "Would you be able to provide this patient's member ID with the TPA?",
                    applicable_when=eq("sections.third_party_administrator.tpa_exists", "Yes"),
                ),
                "tpa_rep_name": text_ask(
                    "TPA Representative Name",
                    "Could you provide the TPA representative name?",
                    applicable_when=eq("sections.third_party_administrator.tpa_exists", "Yes"),
                ),
            },
        ),
        "pharmacy_benefit_manager": Section(
            title="Pharmacy Benefit Manager",
            ask_groups=[
                AskGroup(
                    fields=[
                        "sections.pharmacy_benefit_manager.pbm_name",
                        "sections.pharmacy_benefit_manager.pbm_phone",
                    ],
                    ask="What is the name and contact phone number of the pharmacy benefit manager?",
                )
            ],
            fields={
                "pbm_exists": enum_ask(
                    "PBM Exists", "Does this plan have a pharmacy benefit manager, or PBM?", YES_NO
                ),
                "pbm_name": text_ask(
                    "PBM Name",
                    "What is the name of the pharmacy benefit manager?",
                    required=True,
                    applicable_when=eq("sections.pharmacy_benefit_manager.pbm_exists", "Yes"),
                ),
                "pbm_phone": text_ask(
                    "PBM Phone",
                    "What is the PBM contact phone number?",
                    type_="phone",
                    required=True,
                    applicable_when=eq("sections.pharmacy_benefit_manager.pbm_exists", "Yes"),
                ),
            },
        ),
        "infertility_specialty_pharmacy": Section(
            title="Infertility Specialty Pharmacy",
            ask_groups=[
                AskGroup(
                    fields=[
                        "sections.infertility_specialty_pharmacy.isp_name",
                        "sections.infertility_specialty_pharmacy.isp_phone",
                    ],
                    ask="What is the name and contact phone number of the infertility specialty pharmacy?",
                )
            ],
            fields={
                "isp_exists": enum_ask(
                    "Infertility Specialty Pharmacy Exists",
                    "Does the plan have an infertility specialty pharmacy?",
                    YES_NO,
                    hints=[
                        'Always say "infertility specialty pharmacy" in full; never abbreviate '
                        "to ISP when speaking."
                    ],
                ),
                "isp_name": text_ask(
                    "Infertility Specialty Pharmacy Name",
                    "What is the name of the infertility specialty pharmacy?",
                    required=True,
                    applicable_when=eq("sections.infertility_specialty_pharmacy.isp_exists", "Yes"),
                ),
                "isp_phone": text_ask(
                    "Infertility Specialty Pharmacy Phone",
                    "What is the contact phone number for the infertility specialty pharmacy?",
                    type_="phone",
                    required=True,
                    applicable_when=eq("sections.infertility_specialty_pharmacy.isp_exists", "Yes"),
                ),
            },
        ),
        "insurance_reference_information": Section(
            title="Insurance Reference Information",
            description="Optional reference details about the insurance provider, collected when available.",
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
                "web_portal": text_ask(
                    "Web Portal", "Is there a provider portal URL or access information available?"
                ),
                "web_portal_reference_number": text_ask(
                    "Web Portal Reference Number",
                    "Is there a web portal reference number for this inquiry?",
                ),
                "member_services_pca": text_ask(
                    "Member Services PCA Contact",
                    "Could you provide the Member Services PCA contact?",
                ),
                "employer_name": text_ask(
                    "Employer Name", "May I also have the patient's employer name?"
                ),
            },
        ),
        "insurance_representative": Section(
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
                "ibv_form_type": Leaf(type="text", title="IBV Form Type", role="input"),
            },
        ),
    }


def build_ibv_standard() -> FormSchemaDoc:
    sections: dict[str, Section] = {
        **_context_sections(),
        "insurance_information": _insurance_information(),
        "benefit_coverage": _benefit_coverage(),
        "infertility_treatment": _infertility_treatment(),
        "diagnostic_testing": _diagnostic_testing(),
        "general_coverage": _general_coverage(),
        **_financial_sections(),
        "male_partner_coverage": _male_partner_coverage(),
        **_admin_sections(),
    }

    # Predicate over every prior_auth answer, in document order (a view, not a copy).
    prior_auth_paths: list[str] = []

    def collect(prefix: str, fields: dict[str, FormField]) -> None:
        for key, field in fields.items():
            if isinstance(field, Group):
                collect(f"{prefix}.{key}", field.fields)
            elif key == "prior_auth":
                prior_auth_paths.append(f"{prefix}.{key}")

    for skey, section in sections.items():
        collect(f"sections.{skey}", section.fields)

    return FormSchemaDoc(
        dsl_version="2.1",
        name="IBV Form Standard",
        insurance_type="infertility_treatment",
        description=(
            "Standard infertility benefits verification (IBV) form. Drives UI rendering, "
            "per-task voice-agent prompts, and transcript extraction into field_answer rows."
        ),
        system_fields={
            "chart_number": "sections.patient_information.chart_number",
            "patient_name": "sections.patient_information.patient_name",
            "patient_dob": "sections.patient_information.patient_dob",
            "patient_gender": "sections.patient_information.patient_gender",
            "appointment_date": "sections.appointment_information.appointment_date",
            "appointment_type": "sections.appointment_information.appointment_type",
            "member_id": "sections.insurance_information.policy_number",
            "policy_id": "sections.insurance_information.policy_number",
            "insurance_provider_name": "sections.insurance_reference_information.insurance_provider_name",
            "insurance_provider_phone_number": "sections.insurance_reference_information.insurance_phone_number",
            "verified_by": "sections.verification_information.verified_by",
            "callback_number": "sections.verification_information.callback_number",
            "hospital_name": "sections.hospital_information.hospital_name",
            "hospital_address": "sections.hospital_information.hospital_address",
            "hospital_tax_id": "sections.hospital_information.tax_id",
            "hospital_npi": "sections.hospital_information.npi",
            "doctor_name": "sections.provider_reference_information.provider_name",
            "doctor_npi": "sections.provider_reference_information.npi",
            "form_queued_by": "sections.verification_information.verified_by",
            "form_completed_at": "sections.verification_information.verified_at",
        },
        shared_conditions={
            "family_coverage": eq("sections.benefit_coverage.coverage_type", "Family"),
            "infertility_covered": eq(
                "sections.infertility_treatment.infertility_tx_covered", "Yes"
            ),
            "male_partner_in_scope": AllCondition(
                all=[
                    ref("family_coverage"),
                    eq("sections.patient_information.spouse_gender", "Male"),
                ]
            ),
            "any_service_requires_prior_auth": AnyCondition(
                any=[eq(path, "Yes") for path in prior_auth_paths]
            ),
        },
        sections=sections,
        tasks=[
            Task(
                task_key="insurance_basics",
                title="Insurance Basics",
                outro=(
                    "Perfect, that covers the plan basics. Give me just a moment to note all "
                    "of that down."
                ),
                sections=["insurance_information", "benefit_coverage"],
            ),
            Task(
                task_key="coverage",
                title="Coverage Verification",
                intro="Now I'd like to verify some coverage details.",
                outro=(
                    "Thank you, that's really helpful. One moment while I organize these "
                    "coverage details."
                ),
                sections=["infertility_treatment", "diagnostic_testing", "general_coverage"],
            ),
            Task(
                task_key="financial",
                title="Financial Details",
                intro="Now let me ask about some financial details.",
                outro="Got it — thank you for walking me through those numbers. One moment please.",
                sections=[
                    "deductibles",
                    "out_of_pocket",
                    "lifetime_maximum",
                    "embryo_cryo_storage",
                ],
            ),
            Task(
                task_key="male_partner",
                title="Male Partner Coverage",
                intro="Now I'd like to ask about male partner fertility coverage.",
                outro="Thanks, that covers the male partner benefits. Just a moment.",
                sections=["male_partner_coverage"],
            ),
            Task(
                task_key="closing_admin",
                title="Administrative Details",
                intro="Just a few more questions about administrative details.",
                outro="Perfect, I have all the administrative details I need. One second please.",
                sections=[
                    "enrollment",
                    "authorization_department",
                    "third_party_administrator",
                    "pharmacy_benefit_manager",
                    "infertility_specialty_pharmacy",
                    "insurance_reference_information",
                ],
            ),
            Task(task_key="wrap_up", title="Wrap Up", sections=["insurance_representative"]),
        ],
        flow_rules=[
            FlowRule(
                rule_key="no_out_of_network_coverage",
                when=AllCondition(
                    all=[
                        eq("sections.insurance_information.doctor_inside_network", "No"),
                        eq("sections.insurance_information.facility_inside_network", "No"),
                        eq("sections.insurance_information.out_of_network_coverage", "No"),
                    ]
                ),
                action="terminate_call",
                skip_to_task="wrap_up",
                note=(
                    "Both the doctor and facility are out of network and the plan has no "
                    "out-of-network coverage. Skip all remaining tasks, collect the "
                    "representative name and call reference number, then end the call."
                ),
            )
        ],
        contradictions=[
            Contradiction(
                rule_key="small_group_self_insured_conflict",
                when=AllCondition(
                    all=[
                        eq("sections.benefit_coverage.employer_support_size", "Small Group"),
                        eq("sections.benefit_coverage.plan_fund_type", "Self Insured"),
                    ]
                ),
                fields=[
                    "sections.benefit_coverage.plan_fund_type",
                    "sections.benefit_coverage.employer_support_size",
                ],
                reason="Small group plans are typically fully insured, not self-funded.",
                clarify=(
                    "I want to make sure I have this right — I have the plan as self insured "
                    "and the employer as a small group, but small group plans are typically "
                    "fully insured. Could you double-check whether the plan is self insured or "
                    "fully funded, and whether the employer group is small or large?"
                ),
            ),
            Contradiction(
                rule_key="mandate_requires_infertility_coverage",
                when=AllCondition(
                    all=[
                        eq("sections.benefit_coverage.infertility_plan_mandate", "Yes"),
                        eq("sections.infertility_treatment.infertility_tx_covered", "No"),
                    ]
                ),
                fields=[
                    "sections.infertility_treatment.infertility_tx_covered",
                    "sections.benefit_coverage.infertility_plan_mandate",
                ],
                reason=(
                    "If the plan or state mandates infertility benefits, infertility services "
                    "must be covered under the plan."
                ),
                clarify=(
                    "Earlier you mentioned there is an infertility plan mandate on this policy, "
                    "but infertility treatment is showing as not covered — with a mandate, "
                    "infertility services should be covered. Could you double-check whether "
                    "infertility treatment is covered under this plan?"
                ),
            ),
        ],
    )
