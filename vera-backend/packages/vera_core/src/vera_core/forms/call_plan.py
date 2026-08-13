"""CallPlan — the runtime projection of one published schema + prompt version.

Compiled once, control-plane side, at call dispatch; stored in Redis and loaded
by the DB-free agent worker at call start. A distilled projection — NOT the full
:class:`FormSchemaDoc` — so the worker never re-runs prompt compilation and
UI-only/intake-only content stays out of the runtime surface:

* prompt text comes from :func:`vera_core.forms.prompting.render_task_prompts`
  (session block + one compiled instruction prompt per task, override-merged);
* per-task field descriptors (the Observer's answer schemas) come from
  :func:`vera_core.forms.conditions.leaf_gates`;
* ``flow_rules`` / ``contradictions`` / ``shared_conditions`` are the dsl models
  verbatim, so :func:`vera_core.forms.conditions.evaluate` works unchanged.

Two stages (the split keeps dispatch-pass memoization honest):

* :func:`compile_call_plan` — the per-SCHEMA-VERSION template: ``{{token}}``
  placeholders stay intact; safe to compile once and reuse across forms.
* :func:`fuse_prefill` — the per-FORM stage: hydrates every resolvable
  ``{{token}}`` with the form's intake-prefilled value (spoken-title fallback
  when no value exists), builds the "Known information" block from prefilled
  context-role leaves, and stamps ``prefilled`` — the full per-form ``{path:
  raw value}`` map, all roles. :func:`gating_seed` derives the worker's
  role-scoped gate-evaluation seed from it; the Phase-2 rule engine reads
  ``prefilled`` directly. ``{{current_year}}`` is hydrated here too;
  ``{{value}}`` survives as the runtime sentinel.

Note: PHI tokenization was dropped (2026-07-13), so the fused plan carries the
form's raw intake values — the same Redis posture as ``vera:transcript:*`` keys.
"""

import logging
import re
from collections.abc import Collection, Mapping
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from vera_core.forms.conditions import (
    alternative_pairs,
    has_value,
    is_applicable,
    is_required,
    leaf_gates,
)
from vera_core.forms.dsl import (
    PATH_PREFIX,
    PLACEHOLDER_RE,
    RESERVED_PLACEHOLDER_TOKENS,
    Condition,
    Contradiction,
    FlowRule,
    FormSchemaDoc,
    Group,
    LeafType,
    NumericConsistency,
    RequiredWhen,
    Validation,
    condition_field_paths,
)
from vera_core.forms.prompting import PromptDocument, render_task_prompts
from vera_core.forms.question_plan import (
    PromptPanel,
    PromptQuestion,
    hydrate_panels,
    iter_questions,
    keep_questions,
    map_questions,
)

logger = logging.getLogger(__name__)

COLLECTABLE_ROLES: frozenset[str] = frozenset({"ask", "confirm"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PlanSession(_Model):
    """Session-wide agent text, literal, applied to every task agent."""

    persona: str
    goal: str
    base_instructions: str


class PlanFieldDescriptor(_Model):
    """One collectable leaf: everything the Observer needs to extract its answer."""

    path: str  # root-anchored; byte-identical to field_answer.field_path
    title: str
    type: LeafType
    role: Literal["ask", "confirm"]
    values: list[str] | None = None
    special_values: list[str] | None = None
    validation: Validation | None = None
    required: bool | RequiredWhen = False
    gates: tuple[Condition, ...] = ()
    inapplicable_value: str | None = None
    # `completion_pct_v2` counts a leaf with a default as FILLED and the export writes it, so a
    # worker that cannot see it chases fields the form already calls done.
    default: str | None = None
    # Prose for the Observer when this leaf sits under a routing branch (see `_exclusive_notes`).
    exclusive_note: str | None = None
    # Nearest titled ancestor GROUP — how `still_needed` names this leaf when a fan-out owes
    # only some of its members. None for a leaf sitting directly under its section.
    owner_title: str | None = None


class PlanTask(_Model):
    """One PlanTaskAgent: compiled instruction text + its collectable fields."""

    task_key: str
    title: str
    intro: str | None = None  # AgentTask entry speech — verbatim
    outro: str | None = None  # AgentTask exit speech — verbatim
    prompt: str  # compiled instruction text: lead_in + the question list + trailing
    applicable_when: Condition | None = None
    fields: list[PlanFieldDescriptor] = Field(default_factory=list)
    # `prompt` in its three pieces. The worker re-renders the question list at task entry with
    # the entry-decided gates resolved — a question the gates rule out is DROPPED rather than
    # listed and then retracted — and reassembles it around these, so the flow-rule and
    # contradiction blocks that follow the list survive the narrowing.
    lead_in: str = ""
    panels: list[PromptPanel] = Field(default_factory=list)
    trailing: str = ""


class CallPlan(_Model):
    plan_version: Literal["1"] = "1"
    schema_name: str
    insurance_type: str
    dsl_version: str
    schema_version_id: UUID
    prompt_version_id: UUID | None = None  # None = FACTORY_SESSION fallback ran
    session: PlanSession
    tasks: list[PlanTask]
    flow_rules: list[FlowRule] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    numeric_consistencies: list[NumericConsistency] = Field(default_factory=list)
    shared_conditions: dict[str, Condition] = Field(default_factory=dict)
    # Either/or groups; answering one member satisfies the rest. See `conditions.alternative_pairs`.
    alternative_pairs: list[tuple[str, ...]] = Field(default_factory=list)
    stt_key_terms: list[str] | None = None
    # Per-form stage (fuse_prefill) — empty/None on the compile_call_plan template:
    prefilled: dict[str, Any] = Field(default_factory=dict)  # {path: raw intake value}
    known_information: str | None = None  # "Title: value" lines, context-role leaves only
    on_file_values: str | None = None  # "Title: value" lines, confirm-role prefills (to confirm)


def owed_now(
    task: PlanTask, answers: Mapping[str, Any], shared: Mapping[str, Condition]
) -> list[PromptQuestion]:
    """The task's still-owed SPOKEN questions, in spoken order.

    A join, not a second walk: question identity comes from the tree (so one spoken
    question covering N fields counts once), while gates and requiredness come from the
    descriptors those questions point at. Evaluated PER TARGET because a fan-out's targets
    do not share a gate chain — the IUI copay question spans three CPT codes, each gated on
    its own `covered` — so the question is owed while any covered code still lacks a copay.

    `default` is deliberately not consulted: it declares the value a field takes when not
    collected, never that the question need not be asked. `is_satisfied` keeps reading it,
    so completion percentage, export and intake are unaffected.

    Known limitation: this walks question NODES only, so an end-of-task confirm
    (`confirm_in_task` with `confirm_immediate=False`) can never be owed here — it is spoken
    by `render_task_prompts`'s separate `end_confirms` block, never a node in `task.panels`,
    and `dsl.validate_question_coverage` deliberately exempts it for that reason. Zero such
    leaves exist in either catalog (`test_no_catalog_uses_an_end_of_task_confirm` goes red
    the moment one is authored) — do not union the end-confirm set in here to "fix" this;
    lifting that loop into a named function and unioning it is the deferred follow-up.
    """
    by_path = {field.path: field for field in task.fields}
    owed: list[PromptQuestion] = []
    for question in iter_questions(task.panels):
        if question.routes_between:
            continue  # chooses between panels; collects nothing
        live = [
            field
            for path in question.target_paths
            if (field := by_path.get(path)) is not None
            and is_applicable(field.gates, answers, shared)
        ]
        if any(
            is_required(field, answers, shared) and not has_value(answers, field.path)
            for field in live
        ):
            owed.append(question)
    return owed


def focus_questions(
    task: PlanTask,
    paths: Collection[str],
    answers: Mapping[str, Any],
    shared: dict[str, Condition],
    *,
    explode: bool = False,
) -> list[PromptPanel]:
    """`task`'s question tree narrowed to `paths`, each partly-owed fan-out told which members
    it still needs.

    `explode` grows the set to the transitive closure over gates: a question whose gate reads a
    path being asked comes along, carrying its own `Ask only if …` prose. That pre-loads the
    follow-ups an answer is about to open — the Observer extracts in a detached pass, so on the
    turn right after the representative confirms coverage they are not yet owed, and an agent
    holding an answer with no sanctioned next question is an agent inventing one.

    Named for the operation, not for the owed set: the focused-retry path narrows the same tree
    against a different path set.
    """
    by_path = {field.path: field for field in task.fields}
    wanted = _exploded(task, set(paths), answers, shared, by_path) if explode else set(paths)
    return _stamp_still_needed(keep_questions(task.panels, wanted), wanted, by_path)


def _exploded(
    task: PlanTask,
    wanted: set[str],
    answers: Mapping[str, Any],
    shared: dict[str, Condition],
    by_path: Mapping[str, PlanFieldDescriptor],
) -> set[str]:
    """`wanted` plus every question gated on something already in it, to a fixpoint."""
    questions = [q for q in iter_questions(task.panels) if q.target_paths]
    wanted = set(wanted)
    while True:
        grew = False
        for question in questions:
            if not wanted.isdisjoint(question.target_paths):
                continue
            if all(has_value(answers, path) for path in question.target_paths):
                continue  # on file already; a follow-up nobody owes is noise
            refs = {
                ref
                for path in question.target_paths
                if (field := by_path.get(path)) is not None
                for gate in field.gates
                for ref in condition_field_paths(gate, shared)
            }
            if not wanted.isdisjoint(refs):
                wanted.update(question.target_paths)
                grew = True
        if not grew:
            return wanted


def _stamp_still_needed(
    panels: list[PromptPanel],
    wanted: set[str],
    by_path: Mapping[str, PlanFieldDescriptor],
) -> list[PromptPanel]:
    """`still_needed` on every question owing only SOME of its targets.

    Suppressed unless every owed target has an `owner_title`: a half-named list is worse than
    none, because the agent would read it as the complete remainder."""

    def question(node: PromptQuestion) -> PromptQuestion:
        owed = [path for path in node.target_paths if path in wanted]
        if not owed or len(owed) == len(node.target_paths):
            return node
        titles = [
            title
            for path in owed
            if (field := by_path.get(path)) is not None and (title := field.owner_title)
        ]
        if len(titles) != len(owed):
            return node
        return node.model_copy(update={"still_needed": list(dict.fromkeys(titles))})

    return map_questions(panels, question)


def gating_seed(plan: CallPlan) -> dict[str, Any]:
    """The answers a gate may be judged against before the call has collected anything.

    An `ask`-role leaf is collected ON the call, so a value on file for one is a pre-call
    baseline and never an answer: letting it settle a gate deletes every question behind it
    from the compiled list, which is how the intake UI's `default` for `enrollment_required`
    removed the enrollment provider question from `closing_admin`. `confirm` stays — it is on
    file precisely to be read back — and a path no task collects is clinic-supplied context.

    Provenance is deliberately not consulted, only role: `field_answer.source` does not reach
    the worker, and an ask leaf's authority is the payer's representative either way."""
    asked = {field.path for task in plan.tasks for field in task.fields if field.role == "ask"}
    return {path: value for path, value in plan.prefilled.items() if path not in asked}


def _exclusive_notes(doc: FormSchemaDoc) -> dict[str, str]:
    """`{leaf path: note}` for every leaf under a ROUTING branch — an `alternatives` over groups.

    A routing set picks one branch, but nothing gates the others, so the Observer has been
    inferring `No` for the branch not taken: a live call recorded
    `egg_cryopreservation_elective…covered = "No"` at confidence 90, which asserts the plan does
    not cover elective egg cryopreservation when the representative never discussed it. `N/A` is
    the true answer, and the Observer cannot know that from the transcript alone — the schema has
    to say it.

    Resolved to prose at compile time because the worker is DB-free, the same reason gate
    conditions are pre-rendered rather than shipped as `Condition`s."""
    leaf_paths = [path for path, _leaf, _gates in leaf_gates(doc)]
    notes: dict[str, str] = {}
    for section in doc.sections.values():
        for alternatives in section.alternatives or []:
            branches = [
                (member, node.title)
                for member in alternatives.members
                if isinstance(node := section.fields.get(member.split(".")[-1]), Group)
            ]
            if len(branches) < 2:
                continue  # a leaf-level either/or, not a routing set
            for branch, title in branches:
                others = " or ".join(other for path, other in branches if path != branch)
                note = (
                    f"Only one of {title} or {others} applies to this patient. If the "
                    f"representative indicates {others} applies instead, record N/A here — never "
                    "No, which would claim the plan does not cover it."
                )
                notes.update({p: note for p in leaf_paths if p.startswith(f"{branch}.")})
    return notes


def _owner_titles(doc: FormSchemaDoc) -> dict[str, str]:
    """`{leaf path: nearest titled ancestor group's title}`.

    Sections are deliberately out of reach — `_iter_fields` starts at `section.fields` — because
    the section is the panel a question sits under, not a member of the fan-out."""
    groups = {
        path: field.title
        for path, field in doc._iter_fields()
        if isinstance(field, Group) and field.title
    }
    owners: dict[str, str] = {}
    for path, _leaf in doc.leaf_items():
        parts = path.split(".")
        for cut in range(len(parts) - 1, 1, -1):
            title = groups.get(".".join(parts[:cut]))
            if title is not None:
                owners[path] = title
                break
    return owners


def compile_call_plan(
    doc: FormSchemaDoc,
    prompt_doc: PromptDocument | None,
    *,
    schema_version_id: UUID,
    prompt_version_id: UUID | None,
) -> CallPlan:
    """Compile the per-schema-version CallPlan TEMPLATE (tokens left intact).

    Deterministic: same inputs = identical plan. Task order is document order;
    field order within a task is document order (`leaf_gates`). A confirm leaf
    with `confirm_in_task` belongs to the task it is SPOKEN in, not the task
    owning its (context) section — same routing the prompt renderer uses.
    Run `fuse_prefill` on the result to make the per-form plan.
    """
    rendered = render_task_prompts(doc, prompt_doc)
    section_to_task = doc.section_to_task()
    exclusive_notes = _exclusive_notes(doc)
    owner_titles = _owner_titles(doc)
    fields_by_task: dict[str, list[PlanFieldDescriptor]] = {}
    for path, leaf, gates in leaf_gates(doc):
        if leaf.role not in COLLECTABLE_ROLES:
            continue
        cit = leaf.confirm_in_task
        task_key = cit.task_key if cit is not None else section_to_task.get(path.split(".")[1])
        if task_key is None:
            continue
        assert leaf.role == "ask" or leaf.role == "confirm"  # narrow for the Literal
        fields_by_task.setdefault(task_key, []).append(
            PlanFieldDescriptor(
                path=path,
                title=leaf.title,
                type=leaf.type,
                role=leaf.role,
                values=leaf.values,
                special_values=leaf.special_values,
                validation=leaf.validation,
                required=leaf.required,
                gates=gates,
                inapplicable_value=leaf.inapplicable_value,
                default=leaf.default,
                exclusive_note=exclusive_notes.get(path),
                owner_title=owner_titles.get(path),
            )
        )

    tasks = [
        PlanTask(
            task_key=rendered_task.task_key,
            title=rendered_task.title,
            intro=rendered_task.intro,
            outro=rendered_task.outro,
            prompt=rendered_task.prompt,
            applicable_when=task.applicable_when,
            fields=fields_by_task.get(rendered_task.task_key, []),
            lead_in=rendered_task.lead_in,
            panels=rendered_task.panels,
            trailing=rendered_task.trailing,
        )
        for task, rendered_task in zip(doc.tasks, rendered.tasks, strict=True)
    ]

    return CallPlan(
        schema_name=rendered.name,
        insurance_type=rendered.insurance_type,
        dsl_version=rendered.dsl_version,
        schema_version_id=schema_version_id,
        prompt_version_id=prompt_version_id,
        session=PlanSession(
            persona=rendered.persona,
            goal=rendered.goal,
            base_instructions=rendered.base_instructions,
        ),
        tasks=tasks,
        flow_rules=list(doc.flow_rules or []),
        contradictions=list(doc.contradictions or []),
        numeric_consistencies=list(doc.numeric_consistencies or []),
        shared_conditions=dict(doc.shared_conditions or {}),
        alternative_pairs=alternative_pairs(doc),
        stt_key_terms=doc.stt_key_terms,
    )


def focus_call_plan(plan: CallPlan, paths: Collection[str]) -> CallPlan:
    """Narrow a fused plan to a FOCUSED retry: keep only fields whose path is in
    *paths*, dropping any task left with no fields. The agent then asks ONLY the
    still-missing data points — with no announcement that this is a retry (the
    payer rep must never be told a prior call happened).

    Persona/goal/base_instructions and the ``known_information`` background block
    are preserved (context the agent needs), but ``on_file_values`` is cleared —
    that block drives read-back confirmations of already-known values, exactly the
    "re-verify everything" behavior a focused retry must avoid. A confirm-role
    field kept in *paths* degrades to a plain ask, which is the intended re-collect.
    """
    keep = set(paths)
    tasks = [
        task.model_copy(update={"fields": kept})
        for task in plan.tasks
        if (kept := [f for f in task.fields if f.path in keep])
    ]
    return plan.model_copy(update={"tasks": tasks, "on_file_values": None})


def bookend_paths(plan: CallPlan, reference_field: str) -> list[str]:
    """Field paths a FOCUSED retry must always keep: the opening task's fields (so the
    call still greets and gives the recording/identity disclosure) and the wrap-up
    task's fields (rep name + call reference number). Every call must greet and log its
    OWN reference — the schema's "always run last" contract, and the next retry's focus
    gate reads that reference. The wrap-up task is the one holding *reference_field*."""
    if not plan.tasks:
        return []
    keep = list(plan.tasks[0].fields)
    wrapup = next((t for t in plan.tasks if any(f.path == reference_field for f in t.fields)), None)
    if wrapup is not None:
        keep.extend(wrapup.fields)
    return [f.path for f in keep]


# Two slot forms share one pattern: `{{confirm:<path>}}` keeps the confirm/ask verb label,
# `{{confirm_bare:<path>}}` resolves to the same sentence without it (prompting._confirm_slot
# decides which context emits which).
CONFIRM_SLOT_RE = re.compile(r"\{\{confirm(?P<bare>_bare)?:(?P<path>[\w.]+)\}\}")
_VALUE_TOKEN = "{{value}}"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# "Dr. Dr. Jane" → "Dr. Jane": a title on both the template ("Dr. {{doctor_name}}")
# and the prefilled value ("Dr. Jane Smith") collapses to a single spoken title.
_DOUBLED_HONORIFIC_RE = re.compile(r"\b(Dr|Mr|Mrs|Ms|Prof)\.(?:\s+\1\.)+", re.IGNORECASE)


def _render_value(raw: Any) -> str | None:
    """Prompt-text rendering of a prefilled raw value; None = not renderable
    (absent, or a shape with no sensible spoken form — dict/None)."""
    if isinstance(raw, str):
        text = raw.strip()
        # Mirrors ivr_selection._spoken_value: "N/A" is the intake default and the
        # inapplicable marker, never a value worth speaking.
        if not text or text.upper() == "N/A":
            return None
        return _speak_iso_date(text)
    if isinstance(raw, bool | int | float):
        return str(raw)
    if isinstance(raw, list):
        return ", ".join(str(item) for item in raw)
    return None


def _speak_iso_date(text: str) -> str:
    """An ISO ``YYYY-MM-DD`` renders TTS-friendly ("April 12, 1991"); any other
    string (or a non-calendar date) passes through untouched."""
    if not _ISO_DATE_RE.match(text):
        return text
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return text
    return f"{parsed:%B} {parsed.day}, {parsed.year}"


def _dedupe_honorifics(text: str) -> str:
    return _DOUBLED_HONORIFIC_RE.sub(lambda m: f"{m.group(1)}.", text)


class PrefillFuser:
    """The per-FORM stage, split so a dispatch pass builds the template-invariant
    lookups ONCE per schema version and calls :meth:`fuse` once per form (the
    dispatch loop runs under the per-tenant advisory lock — per-form document
    walks are wasted serialized time). Holds only the fields it needs, never the
    whole doc."""

    def __init__(self, doc: FormSchemaDoc, plan: CallPlan) -> None:
        self._plan = plan
        self._system = doc.system_fields or {}
        self._titles = {path: field.title for path, field in doc._iter_fields()}
        # The Known-information source set: context-role leaves, document order.
        self._context_leaves = [
            (path, leaf.title) for path, leaf in doc.leaf_items() if leaf.role == "context"
        ]
        # Confirm-role leaves: prefilled values the agent must READ BACK to confirm
        # (their prompt is a {{value}} confirmation). Without this block the agent has
        # no value to confirm and degrades to an open ask (risking a conflicting answer).
        # Sentences for the {{confirm:path}} slots live alongside: the compiler emits
        # one per confirm-role leaf and the per-form value decides which is spoken.
        # A confirm-role leaf always carries both texts (dsl.py's Leaf._coherent
        # enforces it), so one pass over leaf_items() builds both structures.
        self._confirm_leaves: list[tuple[str, str]] = []
        self._confirm_prompts: dict[str, tuple[str, str]] = {}
        for path, leaf in doc.leaf_items():
            if leaf.role != "confirm":
                continue
            self._confirm_leaves.append((path, leaf.title))
            if leaf.prompt is not None:
                self._confirm_prompts[path] = (
                    leaf.prompt.confirm or leaf.title,
                    leaf.prompt.ask or leaf.title,
                )

    def fuse(self, values: Mapping[str, Any], *, current_year: int) -> CallPlan:
        """Fuse one form's intake-prefilled values into the template (pure — the
        template is never mutated).

        * Every resolvable ``{{token}}`` (system_fields key or ``sections.…``
          leaf path) in session + task text becomes the form's actual value;
          with no value it falls back to a neutral spoken reference
          ("the <leaf title>"). Reserved tokens (`RESERVED_PLACEHOLDER_TOKENS`):
          ``{{current_year}}`` hydrates here, ``{{value}}`` stays verbatim.
        * ``known_information`` lists prefilled **context-role** leaves ("Title:
          value" lines) — the schema's "context = agent background" contract.
          Ask/confirm leaves are collected/confirmed on the call and
          input/ui_only leaves are never voice-touched, so they stay out.
        * ``prefilled`` carries the full ``{path: raw}`` map (all roles).
          :func:`gating_seed` derives the worker's role-scoped gate-evaluation
          seed from it; the Phase-2 rule engine reads ``prefilled`` directly.
        """
        unresolved = 0
        unbacked = 0

        def expand_slots(text: str) -> str:
            def repl(match: re.Match[str]) -> str:
                nonlocal unbacked
                bare = match.group("bare") is not None
                path = match.group("path")
                sentences = self._confirm_prompts.get(path)
                if sentences is None:
                    # Fail safe: an open ask is never wrong, a fabricated read-back is.
                    unbacked += 1
                    verb, sentence = "ask", self._titles.get(path, path)
                else:
                    confirm_text, ask_text = sentences
                    rendered = _render_value(values.get(path))
                    if rendered is None:
                        verb, sentence = "ask", ask_text
                    else:
                        verb, sentence = "confirm", confirm_text.replace(_VALUE_TOKEN, rendered)
                return sentence if bare else f"{verb} — {sentence}"

            return CONFIRM_SLOT_RE.sub(repl, text)

        def fuse_text(text: str) -> str:
            """Slot expansion then token hydration — the whole per-form rewrite, named once
            because every piece of a task's text gets exactly the same treatment."""
            return hydrate(expand_slots(text)) or ""

        def hydrate(text: str | None) -> str | None:
            if text is None:
                return None

            def repl(match: re.Match[str]) -> str:
                nonlocal unresolved
                token = match.group(1)
                if token in RESERVED_PLACEHOLDER_TOKENS:
                    return str(current_year) if token == "current_year" else match.group(0)
                path = self._system.get(token, token if token.startswith(PATH_PREFIX) else None)
                if path is not None:
                    rendered = _render_value(values.get(path))
                    if rendered is not None:
                        return rendered
                    title = self._titles.get(path)
                    if title is not None:
                        return f"the {title}"
                unresolved += 1
                return match.group(0)

            return _dedupe_honorifics(PLACEHOLDER_RE.sub(repl, text))

        def value_lines(leaves: list[tuple[str, str]]) -> list[str]:
            return [
                f"{title}: {rendered}"
                for path, title in leaves
                if (rendered := _render_value(values.get(path))) is not None
            ]

        known_lines = value_lines(self._context_leaves)
        on_file_lines = value_lines(self._confirm_leaves)

        plan = self._plan
        fused = plan.model_copy(
            update={
                "session": PlanSession(
                    persona=hydrate(plan.session.persona) or "",
                    goal=hydrate(plan.session.goal) or "",
                    base_instructions=hydrate(plan.session.base_instructions) or "",
                ),
                "tasks": [
                    task.model_copy(
                        update={
                            "intro": hydrate(task.intro),
                            "outro": hydrate(task.outro),
                            # The pieces carry the same slots and tokens as `prompt`, and the
                            # worker re-renders the list from `panels` at task entry — so they
                            # get the identical treatment or that re-render speaks a raw
                            # {{confirm:…}} / {{value}}.
                            "prompt": fuse_text(task.prompt),
                            "lead_in": fuse_text(task.lead_in),
                            "panels": hydrate_panels(task.panels, fuse_text),
                            "trailing": fuse_text(task.trailing),
                        }
                    )
                    for task in plan.tasks
                ],
                "prefilled": dict(values),
                "known_information": "\n".join(known_lines) if known_lines else None,
                "on_file_values": "\n".join(on_file_lines) if on_file_lines else None,
            }
        )
        if unresolved:
            # Count only — prompt text may now carry patient values; never log content.
            logger.warning(
                "call plan %s: %d unresolvable placeholder(s) passed through verbatim",
                plan.insurance_type,
                unresolved,
            )
        if unbacked:
            logger.warning(
                "call plan %s: %d confirm slot(s) had no sentence; asked openly instead",
                plan.insurance_type,
                unbacked,
            )
        return fused


def fuse_prefill(
    doc: FormSchemaDoc,
    plan: CallPlan,
    values: Mapping[str, Any],
    *,
    current_year: int,
) -> CallPlan:
    """One-shot convenience over :class:`PrefillFuser` (single-form callers /
    tests). Multi-form callers should build the fuser once per template."""
    return PrefillFuser(doc, plan).fuse(values, current_year=current_year)
