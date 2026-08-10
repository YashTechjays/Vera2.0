"""Deterministic condition → English rendering for compiled task prompts.

One function renders every Condition so wording is uniform across question
gates, task applicability, flow rules and contradictions (2026-07-08 spec §3.2).
"""

from collections import Counter
from collections.abc import Callable

from vera_core.forms.dsl import (
    AllCondition,
    AnyCondition,
    Comparison,
    Condition,
    FormSchemaDoc,
    NotCondition,
    RefCondition,
)


def build_condition_renderer(
    doc: FormSchemaDoc, scope: str | None = None
) -> Callable[[Condition], str]:
    """A renderer bound to one document (title lookup + shared-ref expansion).

    ``scope`` is the root-anchored path of the panel currently being rendered. Inside a
    panel a leaf's disambiguating breadcrumb is redundant — the heading already supplies
    it — so labels drop the shared prefix and a gate that only asks "is the thing this
    panel is about covered?" reads as exactly that. Unscoped output is unchanged, which
    is what flow rules and contradictions still use."""
    leaves = dict(doc.leaf_items())
    title_counts = Counter(leaf.title for leaf in leaves.values())
    shared = doc.shared_conditions or {}
    fields_by_path = dict(doc._iter_fields())  # groups + leaves; intra-package use
    section_titles = {key: section.title for key, section in doc.sections.items()}

    def label(path: str) -> str:
        leaf = leaves.get(path)
        if leaf is None:
            return path
        if scope is not None and path.startswith(f"{scope}."):
            rest = path[len(scope) + 1 :].split(".")
            if len(rest) == 1:
                return f'"{leaf.title}"'
            owner = fields_by_path.get(f"{scope}.{rest[0]}")
            owner_title = owner.title if owner is not None else rest[0]
            return f'{owner_title} "{leaf.title}"'
        if title_counts[leaf.title] == 1:
            return f'"{leaf.title}"'
        parts = path.split(".")  # ["sections", <section>, *groups, <leaf>]
        ancestors = [section_titles.get(parts[1], parts[1])]
        for depth in range(3, len(parts)):
            group = fields_by_path.get(".".join(parts[:depth]))
            if group is not None:
                ancestors.append(group.title)
        return f'"{leaf.title}" ({" › ".join(ancestors)})'  # noqa: RUF001 -- intentional glyph

    def resolve(cond: Condition, seen: frozenset[str]) -> Condition:
        """Follow ref chains (cycle-guarded) so wrapping sees the real shape."""
        while isinstance(cond, RefCondition) and cond.ref in shared and cond.ref not in seen:
            seen = seen | {cond.ref}
            cond = shared[cond.ref]
        return cond

    def wrap(sub: Condition, seen: frozenset[str]) -> str:
        text = render(sub, seen)
        if isinstance(resolve(sub, seen), (AllCondition, AnyCondition)):
            return f"({text})"
        return text

    def render(cond: Condition, seen: frozenset[str] = frozenset()) -> str:
        match cond:
            case Comparison(field=field, op="eq", value=value):
                return f'{label(field)} is "{value}"'
            case Comparison(field=field, op="ne", value=value):
                return f'{label(field)} is not "{value}"'
            case Comparison(field=field, op="in", value=value):
                options = ", ".join(f'"{v}"' for v in value)
                return f"{label(field)} is one of {options}"
            case Comparison(field=field, op="not_in", value=value):
                options = ", ".join(f'"{v}"' for v in value)
                return f"{label(field)} is none of {options}"
            case RefCondition(ref=ref):
                if ref not in shared or ref in seen:
                    return ref
                return render(shared[ref], seen | {ref})
            case AllCondition(all=subs):
                return " and ".join(wrap(sub, seen) for sub in subs)
            case AnyCondition(any=subs):
                return " or ".join(wrap(sub, seen) for sub in subs)
            case NotCondition(not_=sub):
                return f"not ({render(sub, seen)})"
        return ""  # unreachable: the match is exhaustive over Condition

    def entry(cond: Condition) -> str:
        return render(cond)

    return entry


def confirm_slot(path: str, *, bare: bool = False) -> str:
    """The fuse-time slot for a confirm leaf's spoken line — sentence and confirm/ask
    verb are chosen per form, once the prefilled value is known (`call_plan.expand_slots`).
    `bare` drops the `confirm — `/`ask — ` label for the numbered-question list, whose
    numbering already marks it as a question; the bullet contexts keep it because they mix
    confirms and asks, and the label is what disambiguates them."""
    token = "confirm_bare" if bare else "confirm"
    return f"{{{{{token}:{path}}}}}"
