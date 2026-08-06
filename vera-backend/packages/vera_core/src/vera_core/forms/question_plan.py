"""Stage 1 of the task-prompt compiler: schema structure -> a tree of spoken questions.

A *spoken question* is not a stored field. The DSL already says which fields share one
question and which are options on it; this module reads that instead of flattening it:

* ``Section.ask_groups`` — a **fan-out** axis: one question, many target paths ("are these
  eight CPT codes covered?").
* ``Section.alternatives`` over leaves — an **option** axis: one question, one labelled
  sub-bullet per member ("copay or coinsurance?").
* ``Section.alternatives`` over groups — a **routing** question that selects which of the
  panels below applies. ``Alternatives.ask`` was never rendered before this module existed.
* ``Group`` + ``Group.codes`` — a **panel**: a heading, a codes line, and its own numbering.

Stage 2 (``prompting``) renders the tree; every structural decision (which panels earn a
heading, which gates survive as prose) is made here so the renderer stays dumb.

Pure and DB-free. Deterministic: same document = identical tree.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from vera_core.forms.conditions import leaf_gates
from vera_core.forms.dsl import (
    Alternatives,
    AskGroup,
    Codes,
    Condition,
    FormField,
    FormSchemaDoc,
    Group,
    Leaf,
    RequiredWhen,
    Section,
    Task,
    condition_field_paths,
)
from vera_core.forms.prompt_text import build_condition_renderer


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromptOption(_Model):
    """One answerable slot on a question. A plain question has exactly one, unlabelled;
    an alternatives set contributes one per member, labelled with the member's title."""

    label: str | None = None
    answers: str = ""
    date_format: str | None = None
    target_paths: list[str] = Field(default_factory=list)


class PromptQuestion(_Model):
    """One thing the agent says out loud, and every field path it can answer."""

    kind: Literal["question"] = "question"
    text: str
    options: list[PromptOption] = Field(default_factory=list)
    # Residual gate conjuncts only: those the enclosing panel does not already assert, and
    # that the runtime cannot decide at task entry (see `_entry_decided`). Kept structured
    # for the runtime; `gate_text` is the spoken form, resolved here because the worker is
    # DB-free and has no document to render conditions against.
    gates: tuple[Condition, ...] = ()
    gate_text: str | None = None
    derive_text: str | None = None
    required_text: str | None = None
    # Pre-rendered "confirm this immediately after the answer" lines (confirm_in_task).
    immediate_confirms: list[str] = Field(default_factory=list)
    # CPT codes this one question fans out across, when there is more than one.
    fanned_codes: list[str] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    optional: bool = False
    required_when: Condition | None = None
    derive_when: Condition | None = None
    derive_value: str | None = None
    # Routing questions only: the panel titles the answer chooses between.
    routes_between: list[str] = Field(default_factory=list)

    @property
    def target_paths(self) -> list[str]:
        return [path for option in self.options for path in option.target_paths]


class PromptPanel(_Model):
    """A heading the questions underneath belong to — a section, or a service group."""

    kind: Literal["panel"] = "panel"
    # Root-anchored path this panel covers; conditions inside it render relative to this.
    scope: str = ""
    title: str | None = None
    codes: Codes | None = None
    intro: str | None = None
    # Printed once in the header rather than on every question inside it.
    gate: Condition | None = None
    gate_text: str | None = None
    # True for a section-level panel, whose heading renders one level shallower.
    is_section: bool = False
    # Questions and child panels in ONE list because their order is meaningful: a routing
    # question has to sit between the sibling panels it chooses between, not before them all.
    items: list[PromptItem] = Field(default_factory=list)

    @property
    def questions(self) -> list[PromptQuestion]:
        return [item for item in self.items if isinstance(item, PromptQuestion)]

    @property
    def children(self) -> list[PromptPanel]:
        return [item for item in self.items if isinstance(item, PromptPanel)]


PromptItem = Annotated[PromptQuestion | PromptPanel, Field(discriminator="kind")]


def iter_questions(panels: list[PromptPanel]) -> Iterator[PromptQuestion]:
    """Every question in the tree, in spoken order."""
    for panel in panels:
        for item in panel.items:
            if isinstance(item, PromptQuestion):
                yield item
            else:
                yield from iter_questions([item])


def build_question_plan(
    doc: FormSchemaDoc,
    task: Task,
    immediate_by_anchor: dict[str, list[str]] | None = None,
) -> list[PromptPanel]:
    """One panel per collect section of `task`, each holding its spoken questions.

    `immediate_by_anchor` maps an anchor path to already-rendered confirmation lines
    (`confirm_in_task` with `confirm_immediate`), attached to whichever question answers
    that path."""
    return _Builder(doc, task, immediate_by_anchor or {}).build()


def _answers_text(leaf: Leaf) -> str:
    """The answer shape on one compact line: vocabulary, extras, bounds."""
    parts: list[str] = []
    if leaf.values:
        parts.append(" | ".join(leaf.values))
    if leaf.special_values:
        parts.append("also: " + ", ".join(leaf.special_values))
    validation = leaf.validation
    if validation is not None and validation.range is not None:
        rng = validation.range
        if rng.min is not None and rng.max is not None:
            parts.append(f"{rng.min}-{rng.max}")
        elif rng.min is not None:
            parts.append(f"at least {rng.min}")
        else:
            parts.append(f"at most {rng.max}")
    return "; ".join(parts)


class _Builder:
    def __init__(
        self, doc: FormSchemaDoc, task: Task, immediate_by_anchor: dict[str, list[str]]
    ) -> None:
        self.doc = doc
        self.task = task
        self.immediate_by_anchor = immediate_by_anchor
        self.shared = doc.shared_conditions or {}
        self.leaves = dict(doc.leaf_items())
        self.gates = {path: chain for path, _leaf, chain in leaf_gates(doc)}
        self.fields_by_path = dict(doc._iter_fields())
        self.order = {path: i for i, path in enumerate(self.leaves)}
        section_to_task = doc.section_to_task()
        task_index = {t.task_key: i for i, t in enumerate(doc.tasks)}
        # Collectable path -> the task that asks it. Mirrors the worker's `_task_of_path`;
        # a gate referencing only EARLIER tasks is resolved at task entry, never spoken.
        self.task_of_path = {
            path: task_index[key]
            for path in self.leaves
            if (key := section_to_task.get(path.split(".")[1])) is not None
        }
        self.this_task = task_index[task.task_key]

    # -- entry point -----------------------------------------------------------

    def build(self) -> list[PromptPanel]:
        panels: list[PromptPanel] = []
        for section_key in self.task.sections:
            section = self.doc.sections[section_key]
            self._index_section(section_key, section)
            base = f"sections.{section_key}"
            panel = PromptPanel(
                scope=base,
                title=section.title,
                codes=section.codes,
                intro=section.prompt.intro if section.prompt is not None else None,
                gate=section.applicable_when,
                gate_text=(
                    self._render(section.applicable_when, base)
                    if section.applicable_when is not None
                    else None
                ),
                is_section=True,
            )
            asserted = [section.applicable_when] if section.applicable_when is not None else []
            self._fill(panel, base, section.fields, base, asserted, frozenset())
            panels.append(panel)
        return panels

    # -- question assembly (rules 1-6) -----------------------------------------

    def _index_section(self, section_key: str, section: Section) -> None:
        """Build `question_at` (path -> the question emitted there) and `consumed`.

        Union-find over the section's paths, joining members of the same ask group (fan-out)
        and of the same alternatives set (options), so a cost pair riding on top of two ask
        groups collapses into ONE question spanning both fan-outs."""
        self.question_at: dict[str, PromptQuestion] = {}
        self.consumed: set[str] = set()
        self.route_at: dict[str, PromptQuestion] = {}

        group_of: dict[str, AskGroup] = {}
        for ask_group in section.ask_groups or []:
            for member in ask_group.fields:
                group_of[member] = ask_group
        alt_of: dict[str, Alternatives] = {}
        for alternatives in section.alternatives or []:
            for member in alternatives.members:
                alt_of[member] = alternatives

        members = [p for p in self.leaves if p.split(".")[1] == section_key]
        parent: dict[str, str] = {p: p for p in members}

        def find(path: str) -> str:
            while parent[path] != path:
                parent[path] = parent[parent[path]]
                path = parent[path]
            return path

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        for ask_group in section.ask_groups or []:
            for member in ask_group.fields[1:]:
                union(ask_group.fields[0], member)
        for alternatives in section.alternatives or []:
            leaf_members = [m for m in alternatives.members if m in self.leaves]
            for member in leaf_members[1:]:
                union(leaf_members[0], member)
            # a cost pair sitting on two ask groups joins both fan-outs into one question
            for member in leaf_members:
                if member in group_of:
                    union(leaf_members[0], group_of[member].fields[0])
            self._register_route(alternatives)

        classes: dict[str, list[str]] = {}
        for path in members:
            classes.setdefault(find(path), []).append(path)

        for paths in classes.values():
            paths.sort(key=lambda p: self.order[p])
            question = self._question(paths, group_of, alt_of)
            if question is None:
                continue
            self.question_at[paths[0]] = question
            self.consumed.update(paths[1:])

    def _register_route(self, alternatives: Alternatives) -> None:
        """Rule 4: an either/or between GROUPS is a routing question, not a field question."""
        group_members = [m for m in alternatives.members if m not in self.leaves]
        if len(group_members) < 2:
            return
        titles = [self.fields_by_path[m].title for m in group_members]
        text = alternatives.ask or (f"Which of these applies to this plan: {', '.join(titles)}?")
        self.route_at[group_members[0]] = PromptQuestion(text=text, routes_between=titles)

    def _question(
        self,
        paths: list[str],
        group_of: dict[str, AskGroup],
        alt_of: dict[str, Alternatives],
    ) -> PromptQuestion | None:
        head = paths[0]
        leaf = self.leaves[head]
        if leaf.role not in ("ask", "confirm"):
            return None
        if leaf.confirm_in_task is not None:
            return None  # spoken by the confirm block, not the question list

        alternatives = alt_of.get(head)
        ask_group = group_of.get(head)
        if alternatives is not None and alternatives.ask:
            text = alternatives.ask
        elif ask_group is not None:
            text = ask_group.ask
        elif leaf.prompt is not None and leaf.role == "ask" and leaf.prompt.ask:
            text = leaf.prompt.ask
        elif leaf.prompt is not None and leaf.prompt.confirm:
            text = leaf.prompt.confirm
        else:
            text = leaf.title

        # One option per distinct member title, in document order; a single-option question
        # renders its answer shape unlabelled.
        grouped: dict[str, list[str]] = {}
        for path in paths:
            grouped.setdefault(self.leaves[path].title, []).append(path)
        labelled = len(grouped) > 1
        options = [
            PromptOption(
                label=title if labelled else None,
                answers=_answers_text(self.leaves[member_paths[0]]),
                date_format=(
                    validation.date_format
                    if (validation := self.leaves[member_paths[0]].validation) is not None
                    else None
                ),
                target_paths=member_paths,
            )
            for title, member_paths in grouped.items()
        ]

        codes = [
            segment.removeprefix("cpt_")
            for path in paths
            if (segment := path.split(".")[-2]).startswith("cpt_")
        ]
        return PromptQuestion(
            text=text,
            options=options,
            gates=self.gates[head],
            fanned_codes=list(dict.fromkeys(codes)) if len(set(codes)) > 1 else [],
            hints=list(leaf.prompt.hints or []) if leaf.prompt is not None else [],
            optional=all(self.leaves[p].required is False for p in paths),
            required_when=(leaf.required.when if isinstance(leaf.required, RequiredWhen) else None),
            derive_when=leaf.derive.when if leaf.derive is not None else None,
            derive_value=leaf.derive.value if leaf.derive is not None else None,
        )

    # -- panel assembly (rules 7-9) --------------------------------------------

    def _fill(
        self,
        panel: PromptPanel,
        prefix: str,
        fields: dict[str, FormField],
        scope: str,
        asserted: list[Condition],
        panel_codes: frozenset[str],
    ) -> None:
        """Append `fields`' questions and child panels to `panel`, in document order."""
        for key, field in fields.items():
            path = f"{prefix}.{key}"
            if (route := self.route_at.get(path)) is not None:
                panel.items.append(route)
            if isinstance(field, Group):
                self._fill_group(panel, path, field, scope, asserted, panel_codes)
                continue
            if path in self.consumed:
                continue
            question = self.question_at.get(path)
            if question is not None:
                panel.items.append(self._scoped(question, asserted, scope))

    def _fill_group(
        self,
        panel: PromptPanel,
        path: str,
        group: Group,
        scope: str,
        asserted: list[Condition],
        panel_codes: frozenset[str],
    ) -> None:
        if not self._emits(path):
            return  # every question inside fanned out into a sibling's panel question
        own_codes = set((group.codes.cpt or []) if group.codes else ())
        # Rule 8: a node whose codes the enclosing panel already lists, or that hosts only
        # questions answering for its siblings too, is storage — not a subject of its own.
        if (own_codes and own_codes <= panel_codes) or not self._owns_a_question(path):
            self._fill(panel, path, group.fields, scope, asserted, panel_codes)
            return
        if not self._is_panel(group):
            self._fill(panel, path, group.fields, scope, asserted, panel_codes)
            return

        # Rule 9: collapse a wrapper chain downward so the SERVICE names the panel and the
        # per-code groups underneath only contribute their codes.
        top, bottom = path, group
        inner = list(asserted)
        gates: list[Condition] = []
        while True:
            if bottom.applicable_when is not None:
                inner.append(bottom.applicable_when)
                gates.append(bottom.applicable_when)
            nxt = self._sole_emitting_child(bottom, top)
            if nxt is None:
                break
            top, bottom = nxt
        codes = _merge_codes(group.codes, self._harvest_codes(path, group.fields))
        gate = _conjoin(gates)
        child = PromptPanel(
            scope=top,
            title=group.title,
            codes=codes,
            intro=group.prompt.ask if group.prompt is not None and group.prompt.ask else None,
            gate=gate,
            gate_text=self._render(gate, scope) if gate is not None else None,
        )
        panel.items.append(child)
        self._fill(child, top, bottom.fields, top, inner, frozenset(codes.cpt or ()))

    def _scoped(
        self, question: PromptQuestion, asserted: list[Condition], scope: str
    ) -> PromptQuestion:
        """Rules 10 + 12: drop conjuncts the panel already states, and those the runtime
        resolves at task entry. Everything the renderer needs is resolved to text here — the
        worker re-renders this tree and has no document to render conditions against."""
        residual = tuple(
            gate
            for gate in question.gates
            if gate not in asserted and not self._entry_decided(gate)
        )
        confirms = [
            line
            for path in question.target_paths
            for line in self.immediate_by_anchor.get(path, [])
        ]
        return question.model_copy(
            update={
                "gates": residual,
                "gate_text": self._gate_text(residual, question, scope) if residual else None,
                "derive_text": (
                    f"When {self._render(question.derive_when, scope)}: record "
                    f'"{question.derive_value}" without asking.'
                    if question.derive_when is not None
                    else None
                ),
                "required_text": (
                    f"Required only when {self._render(question.required_when, scope)}."
                    if question.required_when is not None
                    else None
                ),
                "immediate_confirms": confirms,
            }
        )

    def _gate_text(
        self, residual: tuple[Condition, ...], question: PromptQuestion, scope: str
    ) -> str:
        """A gate that only asks "is the thing this question is about covered?" says exactly
        that. Field-by-field it reads `"Covered" is "Yes"`, which inside a panel has no
        antecedent — and on a fanned-out question no single field it could name."""
        refs = {ref for gate in residual for ref in condition_field_paths(gate, self.shared)}
        if refs and all(r.endswith(".covered") and r.startswith(f"{scope}.") for r in refs):
            return "the codes above are covered" if len(refs) > 1 else "this service is covered"
        parts: list[str] = []
        for gate in residual:
            text = self._render(gate, scope)
            parts.append(f"({text})" if len(residual) > 1 and " or " in text else text)
        return " and ".join(parts)

    def _render(self, cond: Condition, scope: str) -> str:
        return build_condition_renderer(self.doc, scope)(cond)

    def _entry_decided(self, gate: Condition) -> bool:
        """Every path this conjunct references is collected by an EARLIER task, so its value
        is final when the task is entered: the worker includes or omits the question outright
        and the agent is never asked to re-derive the decision."""
        refs = set(condition_field_paths(gate, self.shared))
        return bool(refs) and all(self.task_of_path.get(ref, -1) < self.this_task for ref in refs)

    # -- structural predicates -------------------------------------------------

    def _emits(self, path: str) -> bool:
        prefix = f"{path}."
        return any(p.startswith(prefix) for p in self.question_at) or any(
            p.startswith(prefix) for p in self.route_at
        )

    def _owns_a_question(self, path: str) -> bool:
        """Is any question here about THIS node alone, rather than a fan-out that also
        answers for its siblings?"""
        prefix = f"{path}."
        return any(
            all(target.startswith(prefix) for target in question.target_paths)
            for p, question in self.question_at.items()
            if p.startswith(prefix)
        ) or any(p.startswith(prefix) for p in self.route_at)

    @staticmethod
    def _is_panel(group: Group) -> bool:
        """A group earns a heading when it names something the rep is asked about: it carries
        codes, or it holds child groups."""
        return group.codes is not None or any(isinstance(f, Group) for f in group.fields.values())

    def _sole_emitting_child(self, group: Group, path: str) -> tuple[str, Group] | None:
        """The one child panel this group is a bare wrapper around, if any."""
        if any(
            f"{path}.{key}" in self.question_at
            for key, field in group.fields.items()
            if isinstance(field, Leaf)
        ):
            return None
        children = [
            (f"{path}.{key}", field)
            for key, field in group.fields.items()
            if isinstance(field, Group) and self._emits(f"{path}.{key}")
        ]
        return children[0] if len(children) == 1 else None

    def _harvest_codes(self, prefix: str, fields: dict[str, FormField]) -> Codes:
        """Codes of every suppressed descendant, merged into the panel speaking for them."""
        cpt: list[str] = []
        icd10: list[str] = []

        def walk(base: str, group_fields: dict[str, FormField]) -> None:
            for key, field in group_fields.items():
                if not isinstance(field, Group):
                    continue
                if field.codes is not None:
                    cpt.extend(c for c in (field.codes.cpt or []) if c not in cpt)
                    icd10.extend(c for c in (field.codes.icd10 or []) if c not in icd10)
                walk(f"{base}.{key}", field.fields)

        walk(prefix, fields)
        return Codes(cpt=cpt or None, icd10=icd10 or None)


def _merge_codes(own: Codes | None, harvested: Codes) -> Codes:
    cpt = list(dict.fromkeys([*((own.cpt if own else None) or []), *(harvested.cpt or [])]))
    icd10 = list(dict.fromkeys([*((own.icd10 if own else None) or []), *(harvested.icd10 or [])]))
    return Codes(cpt=cpt or None, icd10=icd10 or None)


def _conjoin(gates: list[Condition]) -> Condition | None:
    if not gates:
        return None
    if len(gates) == 1:
        return gates[0]
    from vera_core.forms.dsl import AllCondition

    return AllCondition(all=gates)


PromptPanel.model_rebuild()


def drop_questions(panels: list[PromptPanel], excluded: set[str]) -> list[PromptPanel]:
    """The tree minus every question whose targets are ALL in `excluded`, and minus any
    panel left with nothing to ask.

    A question keeping even one askable target stays whole: its options name their own
    paths, and pruning an option would leave the spoken sentence promising an answer slot
    that is no longer listed. A routing question has no targets and survives as long as the
    panels it routes between do."""
    out: list[PromptPanel] = []
    for panel in panels:
        items: list[PromptItem] = []
        for item in panel.items:
            if isinstance(item, PromptPanel):
                items.extend(drop_questions([item], excluded))
            elif not item.target_paths or not set(item.target_paths) <= excluded:
                items.append(item)
        # A routing question with no surviving panel to route into says nothing useful.
        if not any(isinstance(i, PromptPanel) for i in items):
            items = [i for i in items if not (isinstance(i, PromptQuestion) and i.routes_between)]
        if items:
            out.append(panel.model_copy(update={"items": items}))
    return out
