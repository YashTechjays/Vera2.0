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


def build_condition_renderer(doc: FormSchemaDoc) -> Callable[[Condition], str]:
    """A renderer bound to one document (title lookup + shared-ref expansion)."""
    leaves = dict(doc.leaf_items())
    title_counts = Counter(leaf.title for leaf in leaves.values())
    shared = doc.shared_conditions or {}

    def label(path: str) -> str:
        leaf = leaves.get(path)
        if leaf is None:
            return path
        if title_counts[leaf.title] > 1:
            return f'"{leaf.title}" ({path})'
        return f'"{leaf.title}"'

    def wrap(sub: Condition) -> str:
        text = render(sub)
        return f"({text})" if isinstance(sub, (AllCondition, AnyCondition)) else text

    def render(cond: Condition) -> str:
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
                return render(shared[ref]) if ref in shared else ref
            case AllCondition(all=subs):
                return " and ".join(wrap(sub) for sub in subs)
            case AnyCondition(any=subs):
                return " or ".join(wrap(sub) for sub in subs)
            case NotCondition(not_=sub):
                return f"not ({render(sub)})"
        return ""  # unreachable: the match is exhaustive over Condition

    return render
