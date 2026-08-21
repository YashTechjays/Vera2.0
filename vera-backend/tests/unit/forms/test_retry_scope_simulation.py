"""Whole-form retry-scope simulation: the four global invariants a focused retry's ask set
must satisfy — proven by mutating `focus_paths` itself, never by re-deriving expectations from
it (five tests on this branch already shipped passing with their feature removed).

Expectations come only from `vera_core.forms.conditions` primitives (`leaf_gates`,
`is_applicable`, `is_required`) and `FormSchemaDoc`'s own structural accessors — never from
`focus_paths`, `_required_paths`, `expand_to_groups`, `_confirmed`, `is_call_confirmed`, or
`satisfied_required_fraction`. See docs/superpowers/plans/2026-08-21-f-retry-scope-simulation-
suite.md and docs/superpowers/specs/2026-08-21-retry-call-scoping-design.md.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from vera_core.forms.conditions import is_applicable, is_required, is_v2, leaf_gates
from vera_core.forms.dsl import COLLECTED_ROLES, Condition, FormSchemaDoc, Leaf
from vera_core.forms.review import FieldStatus, focus_paths

AUTH, OTHER = uuid4(), uuid4()

_FORM_SCHEMA_DIR = Path(__file__).resolve().parents[3] / "data" / "form_schemas"
_RAW: dict[str, Any] = json.loads((_FORM_SCHEMA_DIR / "ibv_form_standard_v2.json").read_text())
_DOC = FormSchemaDoc.model_validate(_RAW)

Triple = tuple[dict[str, FieldStatus], dict[str, Any], frozenset[UUID]]


@dataclass(frozen=True)
class Scenario:
    """One form's state going into a retry: what is on file, and whether an AUTHORITATIVE
    call collected it."""

    name: str
    values: dict[str, Any]
    confirmed_by_authoritative: bool

    def to_triple(self) -> Triple:
        """The `(status_by_path, values, authoritative_calls)` triple `focus_paths` needs."""
        call_id = AUTH if self.confirmed_by_authoritative else OTHER
        status = {path: FieldStatus("ai_call", True, 95, call_id) for path in self.values}
        return status, dict(self.values), frozenset({AUTH})

    def to_intake_triple(self) -> Triple:
        """Same values, sourced from intake — never confirmed by a call (spec D8)."""
        status = {path: FieldStatus("intake", None, None, None) for path in self.values}
        return status, dict(self.values), frozenset({AUTH})


def _focus(scenario: Scenario, *, intake: bool = False) -> list[str]:
    status, values, calls = scenario.to_intake_triple() if intake else scenario.to_triple()
    return focus_paths(_DOC, status, _RAW, floor=70, values=values, authoritative_calls=calls)


def _owed_now(doc: FormSchemaDoc, scenario: Scenario) -> set[str]:
    """Required + applicable + collectable, and not confirmed by an authoritative call, under
    `scenario.values` — built only from `leaf_gates`/`is_applicable`/`is_required`, so it is
    independent of `focus_paths`. `default` never enters `is_required`/`is_applicable`, so a
    defaulted leaf is included here exactly as the retry rule (`include_defaulted=True`) needs."""
    shared = doc.shared_conditions or {}
    confirmed = set(scenario.values) if scenario.confirmed_by_authoritative else set()
    return {
        path
        for path, leaf, gates in leaf_gates(doc)
        if leaf.role in COLLECTED_ROLES
        and is_applicable(gates, scenario.values, shared)
        and is_required(leaf, scenario.values, shared)
        and path not in confirmed
    }


def _innermost_group(doc: FormSchemaDoc, path: str) -> str | None:
    """The most specific group containing `path`, or `None` for a section-level leaf."""
    best: str | None = None
    for group in doc.group_paths():
        if path.startswith(f"{group}.") and (best is None or len(group) > len(best)):
            best = group
    return best


def _assert_soundness(doc: FormSchemaDoc, scenario: Scenario, focus: list[str]) -> None:
    """I1 — never ask a question that must not be asked."""
    shared = doc.shared_conditions or {}
    gated = list(leaf_gates(doc))
    gates_by_path: dict[str, tuple[Condition, ...]] = {path: gates for path, _leaf, gates in gated}
    roles_by_path = {path: leaf.role for path, leaf, _gates in gated}
    call_scoped = doc.collected_per_call_paths()
    owed = _owed_now(doc, scenario)
    # `expand_to_groups` pulls a group in on ANY owed member without re-checking each member's
    # own gate, so an unconfirmed sibling can legitimately drag in a still-inapplicable one.
    owed_groups = {g for path in owed for g in (_innermost_group(doc, path),) if g is not None}
    for path in focus:
        if path in call_scoped:
            continue
        assert path in roles_by_path, f"focus path is not a leaf at all: {path}"
        assert roles_by_path[path] in COLLECTED_ROLES, f"non-collectable path in focus: {path}"
        if is_applicable(gates_by_path[path], scenario.values, shared):
            continue
        group = _innermost_group(doc, path)
        assert group in owed_groups, (
            f"unsound: {path} has an unsatisfied gate and no owed sibling explains its presence"
        )


def _assert_completeness(doc: FormSchemaDoc, scenario: Scenario, focus: list[str]) -> None:
    """I2 — never skip a question that must be asked, including a defaulted leaf."""
    missing = _owed_now(doc, scenario) - set(focus)
    assert not missing, f"incomplete: {len(missing)} owed path(s) missing from focus"


def _assert_group_closure(doc: FormSchemaDoc, scenario: Scenario, focus: list[str]) -> None:
    """I3 — a partly-owed group is asked whole."""
    shared = doc.shared_conditions or {}
    gates_by_path = {path: gates for path, _leaf, gates in leaf_gates(doc)}
    focus_set = set(focus)
    collectable = doc.collection_paths()
    for group in doc.group_paths():
        prefix = f"{group}."
        members = [path for path in collectable if path.startswith(prefix)]
        if not members or not any(member in focus_set for member in members):
            continue
        for member in members:
            if is_applicable(gates_by_path[member], scenario.values, shared):
                assert member in focus_set, f"group closure gap: {member} missing from {group}"


def _assert_call_scoped_always(doc: FormSchemaDoc, focus: list[str]) -> None:
    """I4 — the three `collected_per='call'` leaves are unconditional."""
    missing = doc.collected_per_call_paths() - set(focus)
    assert not missing, f"call-scoped path(s) missing from focus: {len(missing)}"


def assert_invariants(
    doc: FormSchemaDoc, raw: dict[str, Any], scenario: Scenario, focus: list[str]
) -> None:
    """The four global retry-scope invariants every scenario in this suite must satisfy."""
    assert is_v2(raw)
    _assert_soundness(doc, scenario, focus)
    _assert_completeness(doc, scenario, focus)
    _assert_group_closure(doc, scenario, focus)
    _assert_call_scoped_always(doc, focus)


_FAMILY_SPOUSE_VALUES: dict[str, Any] = {
    "sections.benefit_coverage.coverage_type": "Family",
    "sections.patient_information.spouse_partner_name": "Test Spouse",
    "sections.patient_information.spouse_partner_dob": "1990-01-01",
}

_CALL_SCOPED_VALUES: dict[str, Any] = {
    "sections.patient_verification.is_insurance_active": "Yes",
    "sections.insurance_representative.rep_name": "Test Rep",
    "sections.insurance_representative.call_reference_number": "REF-0001",
}

_SPOUSE_PATHS = frozenset(
    {
        "sections.patient_information.spouse_partner_name",
        "sections.patient_information.spouse_partner_dob",
    }
)

_OFFICE_VISIT_GROUP = "sections.general_coverage.office_visits.cpt_99211"
_OFFICE_VISIT_CONFIRMED_VALUES: dict[str, Any] = {
    f"{_OFFICE_VISIT_GROUP}.covered": "Yes",
    f"{_OFFICE_VISIT_GROUP}.copay": "$25",
    f"{_OFFICE_VISIT_GROUP}.coinsurance": "10%",
}
_OFFICE_VISIT_PATHS = frozenset(
    {
        f"{_OFFICE_VISIT_GROUP}.covered",
        f"{_OFFICE_VISIT_GROUP}.copay",
        f"{_OFFICE_VISIT_GROUP}.coinsurance",
        f"{_OFFICE_VISIT_GROUP}.prior_auth",
    }
)


def test_nothing_on_file() -> None:
    """An empty form still owes only what its own gates allow — the baseline the mutation
    proofs run against."""
    scenario = Scenario("nothing on file", values={}, confirmed_by_authoritative=False)
    assert_invariants(_DOC, _RAW, scenario, _focus(scenario))


def test_family_plan_spouse_details_confirmed_by_authoritative_call() -> None:
    scenario = Scenario(
        "family plan, spouse details confirmed by an authoritative call",
        values=dict(_FAMILY_SPOUSE_VALUES),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _SPOUSE_PATHS.isdisjoint(focus)


def test_family_plan_spouse_details_from_a_non_authoritative_call() -> None:
    """A non-authoritative call is not proof, so the defaulted spouse leaves are still owed."""
    scenario = Scenario(
        "family plan, spouse details from a non-authoritative call",
        values=dict(_FAMILY_SPOUSE_VALUES),
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _SPOUSE_PATHS


def test_family_plan_spouse_details_from_intake() -> None:
    """Spec D8's headline case: an intake value was never put to the payer, so a retry still
    owes it, even though `is_field_satisfied` would call it complete."""
    scenario = Scenario(
        "family plan, spouse details from intake",
        values=dict(_FAMILY_SPOUSE_VALUES),
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario, intake=True)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _SPOUSE_PATHS


def test_one_unconfirmed_group_member_pulls_in_its_already_confirmed_siblings() -> None:
    """I3: `covered`/`copay`/`coinsurance` are confirmed, `prior_auth` is not — group closure
    must re-open the whole panel rather than asking `prior_auth` in isolation."""
    scenario = Scenario(
        "one CPT group member unconfirmed, its siblings already confirmed",
        values=dict(_OFFICE_VISIT_CONFIRMED_VALUES),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _OFFICE_VISIT_PATHS


def test_call_scoped_fields_confirmed_by_authoritative_call_are_still_asked() -> None:
    """I4: the rep's name and the call reference number describe THIS call, so even a fully
    authoritative confirmation from an earlier call must not remove them (spec D8)."""
    scenario = Scenario(
        "call-scoped fields confirmed by an authoritative call",
        values=dict(_CALL_SCOPED_VALUES),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _DOC.collected_per_call_paths() <= set(focus)


def _scoped(focus: list[str], prefix: str) -> set[str]:
    """The subset of `focus` under a dotted path prefix — what the scoped exact-set
    assertions below compare against."""
    return {path for path in focus if path.startswith(prefix)}


def _fill_value(leaf: Leaf) -> str:
    """One syntactically plausible answer for `leaf`, drawn from ITS OWN declared vocabulary
    where one exists (enum) — never a value the schema didn't already sanction."""
    if leaf.type == "enum":
        values = leaf.values or []
        return "Yes" if "Yes" in values else values[0]
    filler_by_type = {
        "currency": "$100",
        "percent": "10%",
        "integer": "1",
        "phone": "555-555-5555",
        "date": "1990-01-01",
    }
    return filler_by_type.get(leaf.type, "Test value")


def _fill_collectable_leaves(doc: FormSchemaDoc, *, prefix: str = "") -> dict[str, Any]:
    """Every ask/confirm leaf under `prefix` (the whole document by default), answered.
    Filling every leaf regardless of its own gate — rather than hand-picking one
    gate-opening path through the schema — silences group closure everywhere it's applied,
    so a test built by deleting a few keys from the result can isolate exactly those keys'
    own behaviour without a stray unconfirmed sibling elsewhere dragging in extra groups."""
    return {
        path: _fill_value(leaf)
        for path, leaf in doc.leaf_items()
        if leaf.role in COLLECTED_ROLES and path.startswith(prefix)
    }


# -- Step 1: coverage-type gating (spouse details) ----------------------------------------

_SPOUSE_VALUES_NO_COVERAGE_TYPE: dict[str, Any] = {
    "sections.patient_information.spouse_partner_name": "Test Spouse",
    "sections.patient_information.spouse_partner_dob": "1990-01-01",
}


def test_individual_plan_has_no_spouse_leaves() -> None:
    """Pins: the spouse leaves are `required when family_coverage` and gated on it too, so an
    Individual plan's `coverage_type` rules the branch out — the `default="N/A"` on each leaf
    never enters this decision."""
    scenario = Scenario(
        "individual plan, spouse leaves absent",
        values={"sections.benefit_coverage.coverage_type": "Individual"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _SPOUSE_PATHS.isdisjoint(focus)


def test_family_plan_spouse_leaves_unanswered_are_owed() -> None:
    """Pins: on a Family plan both spouse leaves are owed although each carries
    `default="N/A"` — the retry ask set includes defaulted leaves, so a default must not
    excuse them."""
    scenario = Scenario(
        "family plan, spouse leaves unanswered",
        values={"sections.benefit_coverage.coverage_type": "Family"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) >= _SPOUSE_PATHS


def test_coverage_type_unanswered_hides_spouse_leaves_even_with_values_on_file() -> None:
    """Pins the gate-parent case: with `coverage_type` itself unanswered, `family_coverage` is
    unsatisfied, so both spouse leaves stay ABSENT even though a value is already on file for
    each — recovering the gate parent belongs to `focus_questions(explode=True)` in the
    compiled plan, not to `focus_paths`."""
    scenario = Scenario(
        "coverage_type unanswered, spouse values already on file",
        values=dict(_SPOUSE_VALUES_NO_COVERAGE_TYPE),
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _SPOUSE_PATHS.isdisjoint(focus)


# -- Step 2: plan-type gating (PCP referral) ----------------------------------------------

_PLAN_TYPE_PATH = "sections.insurance_information.plan_type"
_PCP_REFERRAL_PATH = "sections.benefit_coverage.pcp_referral_required"


def test_ppo_plan_has_no_pcp_referral_question() -> None:
    """Pins: `pcp_referral_required` is gated on `plan_type == "HMO"`, so a PPO plan never
    asks it, `default="N/A"` notwithstanding."""
    scenario = Scenario(
        "PPO plan", values={_PLAN_TYPE_PATH: "PPO"}, confirmed_by_authoritative=False
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _PCP_REFERRAL_PATH not in focus


def test_hmo_plan_pcp_referral_unanswered_is_owed() -> None:
    scenario = Scenario(
        "HMO plan, PCP referral unanswered",
        values={_PLAN_TYPE_PATH: "HMO"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _PCP_REFERRAL_PATH in focus


def test_hmo_plan_pcp_referral_confirmed_by_authoritative_call_is_absent() -> None:
    scenario = Scenario(
        "HMO plan, PCP referral confirmed by an authoritative call",
        values={_PLAN_TYPE_PATH: "HMO", _PCP_REFERRAL_PATH: "Yes"},
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _PCP_REFERRAL_PATH not in focus


def test_hmo_plan_pcp_referral_confirmed_by_non_authoritative_call_is_still_owed() -> None:
    """Spec D8: a non-authoritative call is not proof, so the retry still owes it."""
    scenario = Scenario(
        "HMO plan, PCP referral confirmed by a non-authoritative call",
        values={_PLAN_TYPE_PATH: "HMO", _PCP_REFERRAL_PATH: "Yes"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _PCP_REFERRAL_PATH in focus


# -- Step 3: two-level gating (male partner) ----------------------------------------------

_COVERAGE_TYPE_PATH = "sections.benefit_coverage.coverage_type"
_SPOUSE_GENDER_PATH = "sections.patient_information.spouse_gender"
_MALE_PARTNER_PREFIX = "sections.male_partner_coverage."
_MALE_PARTNER_COVERED_PATH = f"{_MALE_PARTNER_PREFIX}male_partner_covered"
_SEMEN_ANALYSIS_GROUP = f"{_MALE_PARTNER_PREFIX}semen_analysis.cpt_89320"
_SPERM_CRYO_GROUP = f"{_MALE_PARTNER_PREFIX}sperm_cryopreservation.cpt_89259"
_CPT_GROUP_LEAVES = ("covered", "copay", "coinsurance", "prior_auth")
_SEMEN_ANALYSIS_LEAVES = frozenset(f"{_SEMEN_ANALYSIS_GROUP}.{f}" for f in _CPT_GROUP_LEAVES)
_SPERM_CRYO_LEAVES = frozenset(f"{_SPERM_CRYO_GROUP}.{f}" for f in _CPT_GROUP_LEAVES)


def test_individual_plan_hides_whole_male_partner_subtree() -> None:
    """Pins level one of the two-level gate: `male_partner_in_scope` needs `family_coverage`
    first, so an Individual plan rules out the whole `male_partner_coverage` section
    regardless of the spouse's gender."""
    scenario = Scenario(
        "individual plan, spouse marked male",
        values={_COVERAGE_TYPE_PATH: "Individual", _SPOUSE_GENDER_PATH: "Male"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _MALE_PARTNER_PREFIX) == set()


def test_family_plan_female_spouse_hides_whole_male_partner_subtree() -> None:
    """Pins level two: `family_coverage` alone is not enough — the ref also needs
    `spouse_gender == "Male"`, so a Family plan with a female spouse still rules the whole
    section out."""
    scenario = Scenario(
        "family plan, spouse marked female",
        values={_COVERAGE_TYPE_PATH: "Family", _SPOUSE_GENDER_PATH: "Female"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _MALE_PARTNER_PREFIX) == set()


def test_male_partner_in_scope_asks_only_the_gate_leaf() -> None:
    """With both levels of the gate open and `male_partner_covered` itself unanswered, the
    panels behind it (each gated on `male_partner_covered == "Yes"`) are not yet applicable —
    nothing owed drags them into focus via group closure, so only the gate leaf is asked."""
    scenario = Scenario(
        "family plan, male spouse, male_partner_covered unanswered",
        values={_COVERAGE_TYPE_PATH: "Family", _SPOUSE_GENDER_PATH: "Male"},
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _MALE_PARTNER_PREFIX) == {_MALE_PARTNER_COVERED_PATH}


def test_male_partner_covered_confirmed_yes_opens_both_panels_in_full() -> None:
    """With `male_partner_covered` authoritatively confirmed "Yes", both the `semen_analysis`
    and `sperm_cryopreservation` CPT panels become applicable and their `covered` leaf is
    owed, so group closure opens each panel whole (all four leaves, including `copay`/
    `coinsurance` which are not yet individually applicable — expected, see module docstring's
    corrected I1). The gate leaf itself, now confirmed, drops out."""
    scenario = Scenario(
        "family plan, male spouse, male_partner_covered confirmed Yes",
        values={
            _COVERAGE_TYPE_PATH: "Family",
            _SPOUSE_GENDER_PATH: "Male",
            _MALE_PARTNER_COVERED_PATH: "Yes",
        },
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _MALE_PARTNER_PREFIX) == _SEMEN_ANALYSIS_LEAVES | _SPERM_CRYO_LEAVES


# -- Step 4: existence-flag gating (TPA, PBM, ISP, enrollment) ----------------------------


@dataclass(frozen=True)
class _ExistenceFlagCase:
    """One existence-flag section: the ungated flag and the leaf(ves) gated on it.

    `third_party_administrator` has no `tpa_phone` counterpart to `tpa_name` in this schema
    (verified against the compiled JSON), so its `detail_paths` is a singleton unlike its three
    siblings."""

    label: str
    flag: str
    section_prefix: str
    detail_paths: frozenset[str]
    always_owed: frozenset[str] = frozenset()


_EXISTENCE_FLAG_CASES = [
    _ExistenceFlagCase(
        "third_party_administrator",
        "sections.third_party_administrator.tpa_exists",
        "sections.third_party_administrator.",
        frozenset({"sections.third_party_administrator.tpa_name"}),
    ),
    _ExistenceFlagCase(
        "pharmacy_benefit_manager",
        "sections.pharmacy_benefit_manager.pbm_exists",
        "sections.pharmacy_benefit_manager.",
        frozenset(
            {
                "sections.pharmacy_benefit_manager.pbm_name",
                "sections.pharmacy_benefit_manager.pbm_phone",
            }
        ),
    ),
    _ExistenceFlagCase(
        "infertility_specialty_pharmacy",
        "sections.infertility_specialty_pharmacy.isp_exists",
        "sections.infertility_specialty_pharmacy.",
        frozenset(
            {
                "sections.infertility_specialty_pharmacy.isp_name",
                "sections.infertility_specialty_pharmacy.isp_phone",
            }
        ),
    ),
    _ExistenceFlagCase(
        "enrollment",
        "sections.enrollment.enrollment_required",
        "sections.enrollment.",
        frozenset(
            {
                "sections.enrollment.enrollment_provider_name",
                "sections.enrollment.enrollment_provider_phone",
            }
        ),
        always_owed=frozenset({"sections.enrollment.center_of_excellence_required"}),
    ),
]


@pytest.mark.parametrize(
    "case", _EXISTENCE_FLAG_CASES, ids=[c.label for c in _EXISTENCE_FLAG_CASES]
)
def test_existence_flag_gating(case: _ExistenceFlagCase) -> None:
    """Pins the four identically-shaped existence-flag sections: unanswered asks only the
    flag; a flag authoritatively confirmed "No" asks nothing; confirmed "Yes" asks only the
    detail leaf(ves). `enrollment` additionally carries `center_of_excellence_required`,
    ungated and never answered here, so it is owed in all three sub-cases — `always_owed`
    accounts for it rather than letting it read as a surprise."""
    unanswered = Scenario(
        f"{case.label}, flag unanswered", values={}, confirmed_by_authoritative=False
    )
    focus = _focus(unanswered)
    assert_invariants(_DOC, _RAW, unanswered, focus)
    assert _scoped(focus, case.section_prefix) == {case.flag} | case.always_owed

    confirmed_no = Scenario(
        f"{case.label}, flag confirmed No",
        values={case.flag: "No"},
        confirmed_by_authoritative=True,
    )
    focus = _focus(confirmed_no)
    assert_invariants(_DOC, _RAW, confirmed_no, focus)
    assert _scoped(focus, case.section_prefix) == case.always_owed

    confirmed_yes = Scenario(
        f"{case.label}, flag confirmed Yes",
        values={case.flag: "Yes"},
        confirmed_by_authoritative=True,
    )
    focus = _focus(confirmed_yes)
    assert_invariants(_DOC, _RAW, confirmed_yes, focus)
    assert _scoped(focus, case.section_prefix) == case.detail_paths | case.always_owed


# -- Step 5: the 27-way prior-auth rollup -------------------------------------------------

_AUTH_DEPT_PREFIX = "sections.authorization_department."
_AUTH_DEPT_PATHS = frozenset(
    {
        "sections.authorization_department.auth_department_name",
        "sections.authorization_department.auth_department_phone",
    }
)


def test_no_service_requiring_prior_auth_hides_authorization_department() -> None:
    scenario = Scenario(
        "no service requires prior auth", values={}, confirmed_by_authoritative=False
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _AUTH_DEPT_PREFIX) == set()


@dataclass(frozen=True)
class _PriorAuthDisjunct:
    label: str
    values: dict[str, Any]


_PRIOR_AUTH_DISJUNCTS = [
    _PriorAuthDisjunct(
        "diagnostic labs/xray/ultrasound CPT 58340",
        {
            "sections.diagnostic_testing.diagnostic_testing_covered": "Yes",
            "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.covered": "Yes",
            "sections.diagnostic_testing.labs_xray_ultrasound.cpt_58340.prior_auth": "Yes",
        },
    ),
    _PriorAuthDisjunct(
        "male partner semen analysis CPT 89320",
        {
            _COVERAGE_TYPE_PATH: "Family",
            _SPOUSE_GENDER_PATH: "Male",
            _MALE_PARTNER_COVERED_PATH: "Yes",
            f"{_SEMEN_ANALYSIS_GROUP}.covered": "Yes",
            f"{_SEMEN_ANALYSIS_GROUP}.prior_auth": "Yes",
        },
    ),
]


@pytest.mark.parametrize(
    "case", _PRIOR_AUTH_DISJUNCTS, ids=[c.label for c in _PRIOR_AUTH_DISJUNCTS]
)
def test_single_service_prior_auth_opens_authorization_department(
    case: _PriorAuthDisjunct,
) -> None:
    """Pins that `any_service_requires_prior_auth` is a real 27-way `any`: two unrelated
    disjuncts, each with its own deeper gate chain satisfied, both open the authorization
    department leaves — proving the rollup isn't keyed to one hardcoded field."""
    scenario = Scenario(case.label, values=dict(case.values), confirmed_by_authoritative=True)
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _AUTH_DEPT_PREFIX) == _AUTH_DEPT_PATHS


# -- Step 6: money sentinel gating ---------------------------------------------------------


@dataclass(frozen=True)
class _MoneyTripletCase:
    label: str
    total_path: str
    met_path: str
    remaining_path: str
    sentinel: str
    real_amount: str


_MONEY_TRIPLET_CASES = [
    _MoneyTripletCase(
        "deductibles.individual",
        "sections.deductibles.individual.total",
        "sections.deductibles.individual.met_amount",
        "sections.deductibles.individual.remaining",
        sentinel="No Deductible",
        real_amount="$500",
    ),
    _MoneyTripletCase(
        "out_of_pocket.individual",
        "sections.out_of_pocket.individual.total",
        "sections.out_of_pocket.individual.met_amount",
        "sections.out_of_pocket.individual.remaining",
        sentinel="$0",
        real_amount="$1000",
    ),
    _MoneyTripletCase(
        "lifetime_maximum",
        "sections.lifetime_maximum.total",
        "sections.lifetime_maximum.met_amount",
        "sections.lifetime_maximum.remaining",
        sentinel="No Limit",
        real_amount="$5000",
    ),
]


@pytest.mark.parametrize("case", _MONEY_TRIPLET_CASES, ids=[c.label for c in _MONEY_TRIPLET_CASES])
def test_money_sentinel_gating(case: _MoneyTripletCase) -> None:
    """Pins each triplet's OWN sentinel list gating `met_amount`/`remaining`: a sentinel total
    (unique to that triplet's list, e.g. "No Deductible" only appears in `deductibles`) closes
    both; a real, authoritatively confirmed total leaves both owed."""
    sentinel_scenario = Scenario(
        f"{case.label}, total is a sentinel",
        values={case.total_path: case.sentinel},
        confirmed_by_authoritative=True,
    )
    focus = _focus(sentinel_scenario)
    assert_invariants(_DOC, _RAW, sentinel_scenario, focus)
    assert case.met_path not in focus
    assert case.remaining_path not in focus

    real_amount_scenario = Scenario(
        f"{case.label}, total is a real amount",
        values={case.total_path: case.real_amount},
        confirmed_by_authoritative=True,
    )
    focus = _focus(real_amount_scenario)
    assert_invariants(_DOC, _RAW, real_amount_scenario, focus)
    assert case.met_path in focus
    assert case.remaining_path in focus


# -- Task 3 step 1: group closure ----------------------------------------------------------

_DIAGNOSTIC_COVERED_PATH = "sections.diagnostic_testing.diagnostic_testing_covered"
_LABS_PREFIX = "sections.diagnostic_testing.labs_xray_ultrasound."
_LABS_CPT_CODES = (
    "cpt_58340",
    "cpt_82670",
    "cpt_83001",
    "cpt_83002",
    "cpt_84146",
    "cpt_84443",
    "cpt_84144",
    "cpt_76830",
)


def _labs_panel_values() -> dict[str, Any]:
    """Every collectable leaf of the 32-member `labs_xray_ultrasound` panel, answered — the
    baseline the group-closure tests below poke one hole in."""
    values: dict[str, Any] = {_DIAGNOSTIC_COVERED_PATH: "Yes"}
    for code in _LABS_CPT_CODES:
        values[f"{_LABS_PREFIX}{code}.covered"] = "Yes"
        values[f"{_LABS_PREFIX}{code}.copay"] = "$25"
        values[f"{_LABS_PREFIX}{code}.coinsurance"] = "10%"
        values[f"{_LABS_PREFIX}{code}.prior_auth"] = "No"
    return values


def test_one_missing_leaf_in_the_32_member_labs_panel_pulls_the_whole_panel() -> None:
    """The product owner's headline case, at the schema's largest panel: `expand_to_groups`
    matches every ancestor group whose subtree contains a wanted path, so one unconfirmed leaf
    inside `cpt_58340` reopens the whole 32-member `labs_xray_ultrasound` panel, not just
    `cpt_58340`'s own 4.

    `prior_auth`, not the brief's `copay`: the compiled schema's `alternatives` block flattens
    `copay`/`coinsurance` into one 16-member either/or set across all 8 CPT codes in this
    panel (one pair per code), so leaving `copay` alone unconfirmed while `coinsurance` stays
    confirmed would not make it owed on its own account — see the either/or tests below — and
    would not have exercised group closure at all. `prior_auth` carries no such partner."""
    values = _labs_panel_values()
    missing = f"{_LABS_PREFIX}cpt_58340.prior_auth"
    del values[missing]
    scenario = Scenario(
        "labs_xray_ultrasound panel, cpt_58340.prior_auth unconfirmed",
        values=values,
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    panel = {p for p in _DOC.collection_paths() if p.startswith(_LABS_PREFIX)}
    assert len(panel) == 32
    assert _scoped(focus, _LABS_PREFIX) == panel


def test_labs_panel_fully_confirmed_contributes_nothing_to_focus() -> None:
    """The converse: with every path in the panel authoritatively confirmed, no member is
    owed, so group closure has nothing to expand and the panel is silent."""
    scenario = Scenario(
        "labs_xray_ultrasound panel, fully confirmed",
        values=_labs_panel_values(),
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _scoped(focus, _LABS_PREFIX) == set()


_ASC_FACILITY_PREFIX = "sections.general_coverage.asc_facility.cpt_58555."


def test_one_missing_leaf_in_a_different_small_group_also_pulls_the_whole_group() -> None:
    """Pins that group closure isn't a property of the one freak 32-member panel: the same
    single-missing-leaf rule reopens a plain 4-member CPT group too, so the first test isn't
    one lucky panel."""
    values: dict[str, Any] = {
        f"{_ASC_FACILITY_PREFIX}covered": "Yes",
        f"{_ASC_FACILITY_PREFIX}copay": "$25",
        f"{_ASC_FACILITY_PREFIX}coinsurance": "10%",
    }
    scenario = Scenario(
        "asc_facility.cpt_58555, prior_auth unconfirmed",
        values=values,
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    group = {p for p in _DOC.collection_paths() if p.startswith(_ASC_FACILITY_PREFIX)}
    assert len(group) == 4
    assert _scoped(focus, _ASC_FACILITY_PREFIX) == group


# -- Task 3 step 2: either/or sets ----------------------------------------------------------

_INFERTILITY_PREFIX = "sections.infertility_treatment."
_OVULATION_PREFIX = f"{_INFERTILITY_PREFIX}ovulation_induction."
_OVULATION_COPAY_PATH = f"{_OVULATION_PREFIX}copay"
_OVULATION_COINSURANCE_PATH = f"{_OVULATION_PREFIX}coinsurance"
_OVULATION_PRIOR_AUTH_PATH = f"{_OVULATION_PREFIX}prior_auth"

# `infertility_tx_covered` gates EIGHT sibling panels (ovulation_induction is only one), so
# every either/or scenario below starts from a full-section fill and deletes only the leaf(ves)
# under test — otherwise an untouched sibling panel's own unconfirmed leaves would drag their
# groups into focus too, via the very ancestor-group closure `_assert_soundness` already
# expects (see its docstring), just for a panel this test never meant to exercise.
_INFERTILITY_SECTION_FILLED = _fill_collectable_leaves(_DOC, prefix=_INFERTILITY_PREFIX)


def test_ovulation_induction_neither_copay_nor_coinsurance_answered_both_owed() -> None:
    """Neither side of the either/or pair has an answer, so both are genuinely owed — the
    baseline the two `copay`-confirmed cases below contrast against."""
    values = dict(_INFERTILITY_SECTION_FILLED)
    del values[_OVULATION_COPAY_PATH]
    del values[_OVULATION_COINSURANCE_PATH]
    scenario = Scenario(
        "ovulation induction, neither copay nor coinsurance answered",
        values=values,
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _OVULATION_COPAY_PATH in focus
    assert _OVULATION_COINSURANCE_PATH in focus


def test_ovulation_induction_copay_confirmed_coinsurance_reappears_via_group_closure() -> None:
    """`copay` and `coinsurance` are one either/or pair (the schema's `alternatives` block):
    one answer satisfies the pair, so `coinsurance` is NOT owed on its OWN account here. It
    still shows up in `focus` below — but via `expand_to_groups`, because `prior_auth` (unrelated
    to the pair) is also left unanswered and pulls the whole group back open. This documents
    that interaction rather than hiding it: `prior_auth`'s presence is the actual reason
    `coinsurance` is back, not the either/or rule."""
    values = dict(_INFERTILITY_SECTION_FILLED)
    del values[_OVULATION_COINSURANCE_PATH]
    del values[_OVULATION_PRIOR_AUTH_PATH]
    scenario = Scenario(
        "ovulation induction, copay confirmed, coinsurance and prior_auth unanswered",
        values=values,
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert _OVULATION_PRIOR_AUTH_PATH in focus  # owed on its own account: pulls the group open
    assert _OVULATION_COINSURANCE_PATH in focus  # present only because the group reopened


# -- Task 3 step 3: the two whole-set extremes -----------------------------------------------

_ALL_LEAVES_FILLED = _fill_collectable_leaves(_DOC)


def test_everything_confirmed_by_an_authoritative_call_leaves_only_call_scoped_paths() -> None:
    """The decisive extreme: every collectable leaf in the document (182 of them) is answered
    AND authoritatively confirmed, so nothing is owed anywhere and group closure has nothing to
    expand. `focus` shrinks to exactly the three `collected_per="call"` paths (I4)."""
    scenario = Scenario(
        "every collectable leaf confirmed by an authoritative call",
        values=_ALL_LEAVES_FILLED,
        confirmed_by_authoritative=True,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    assert set(focus) == _DOC.collected_per_call_paths()
    assert len(focus) == 3


def test_everything_answered_by_a_non_authoritative_call_nothing_is_skipped() -> None:
    """Spec D8's whole point at whole-document scale: every leaf has an answer on file, but
    from a call that isn't authoritative — not proof, so the retry still owes every
    required-applicable-collectable path, exactly as if nothing were on file, plus the three
    call-scoped paths. (Group closure adds a further handful of non-required siblings on top
    of that set, so `<=` containment — not equality — is the honest claim here.)"""
    scenario = Scenario(
        "every collectable leaf answered by a non-authoritative call",
        values=_ALL_LEAVES_FILLED,
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario)
    assert_invariants(_DOC, _RAW, scenario, focus)
    expected = _owed_now(_DOC, scenario) | _DOC.collected_per_call_paths()
    assert expected <= set(focus)


def test_everything_supplied_by_intake_nothing_is_skipped() -> None:
    """Spec D8's headline case at whole-document scale: an intake value was never put to the
    payer, so it carries no more trust than nothing on file at all — the same owed set as the
    non-authoritative-call extreme above, reached through a different `AnswerSource`."""
    scenario = Scenario(
        "every collectable leaf supplied by intake",
        values=_ALL_LEAVES_FILLED,
        confirmed_by_authoritative=False,
    )
    focus = _focus(scenario, intake=True)
    assert_invariants(_DOC, _RAW, scenario, focus)
    expected = _owed_now(_DOC, scenario) | _DOC.collected_per_call_paths()
    assert expected <= set(focus)
    non_authoritative_focus = _focus(scenario)
    assert set(focus) == set(non_authoritative_focus)
