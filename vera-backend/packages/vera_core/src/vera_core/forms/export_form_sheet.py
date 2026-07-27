"""UI-replica "Form" sheet for the XLSX export — a Python port of the
frontend's schema-derived layout rules.

The sheet carries PHI (field values): callers stream it inside an authed,
audited, no-store response. This module must never log.

Layout sources of truth (update together):
- placement / flow: vera-frontend/src/components/ibv/SchemaForm.tsx
- matrix model:     vera-frontend/src/lib/ibv/schema.ts (getSectionTable)
- grid headers:     vera-frontend/src/components/ibv/SectionMatrix.tsx
"""

from __future__ import annotations

from dataclasses import dataclass

from vera_core.forms.dsl import Group, Leaf, Section


@dataclass(frozen=True)
class TableCell:
    path: str
    leaf: Leaf


@dataclass(frozen=True)
class TableRow:
    path: str
    label: str
    cells: dict[str, TableCell]


@dataclass(frozen=True)
class TableGroup:
    path: str
    label: str
    icd10: str
    rows: list[TableRow]
    extras: dict[str, TableCell]


@dataclass(frozen=True)
class SectionTable:
    columns: list[tuple[str, str]]  # (leaf key, column title)
    extra_columns: list[tuple[str, str]]  # (leaf key, column title)
    has_icd: bool
    groups: list[TableGroup]
    leaves: list[tuple[str, Leaf]]  # section-level (path, leaf) field rows


def _leaf_entries(g: Group | Section) -> list[tuple[str, Leaf]]:
    return [(k, f) for k, f in g.fields.items() if isinstance(f, Leaf)]


def _group_entries(g: Group | Section) -> list[tuple[str, Group]]:
    return [(k, f) for k, f in g.fields.items() if isinstance(f, Group)]


def section_table(section_key: str, section: Section) -> SectionTable | None:
    """Matrix model for a ``ui.layout: "table"`` section — the layout hint alone
    decides. Top-level groups are bands; their subgroups are rows and their own
    leaves are per-group rowspan extras; a subgroup-less group is itself one row
    labelled by its CPT codes, its leaves split row-cells/extras by key."""
    if section.ui is None or section.ui.layout != "table":
        return None
    base = f"sections.{section_key}"
    top_leaves = [(f"{base}.{k}", f) for k, f in _leaf_entries(section)]
    top_groups = _group_entries(section)

    extra_titles: dict[str, str] = {}
    for _, g in top_groups:
        if not _group_entries(g):
            continue
        for k, f in _leaf_entries(g):
            extra_titles.setdefault(k, f.title)

    column_titles: dict[str, str] = {}
    groups: list[TableGroup] = []
    for gkey, g in top_groups:
        gpath = f"{base}.{gkey}"
        subgroups = _group_entries(g)
        extras: dict[str, TableCell] = {}
        rows: list[TableRow] = []
        if subgroups:
            for k, f in _leaf_entries(g):
                extras[k] = TableCell(f"{gpath}.{k}", f)
            for rkey, r in subgroups:
                rpath = f"{gpath}.{rkey}"
                entries = _leaf_entries(r)
                for k, f in entries:
                    column_titles.setdefault(k, f.title)
                rows.append(
                    TableRow(
                        rpath,
                        r.title,
                        {k: TableCell(f"{rpath}.{k}", f) for k, f in entries},
                    )
                )
        else:
            entries = _leaf_entries(g)
            row_entries = [(k, f) for k, f in entries if k not in extra_titles]
            for k, f in row_entries:
                column_titles.setdefault(k, f.title)
            for k, f in entries:
                if k in extra_titles:
                    extras[k] = TableCell(f"{gpath}.{k}", f)
            cpt = ", ".join(g.codes.cpt) if g.codes and g.codes.cpt else "—"
            rows.append(
                TableRow(
                    gpath,
                    cpt,
                    {k: TableCell(f"{gpath}.{k}", f) for k, f in row_entries},
                )
            )
        icd = ", ".join(g.codes.icd10) if g.codes and g.codes.icd10 else ""
        groups.append(TableGroup(gpath, g.title, icd, rows, extras))

    return SectionTable(
        columns=list(column_titles.items()),
        extra_columns=list(extra_titles.items()),
        has_icd=any(g.icd10 for g in groups),
        groups=groups,
        leaves=top_leaves,
    )
