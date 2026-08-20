import { useState } from "react"
import { CheckCheck, ChevronDown } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { FieldRow } from "./FieldRow"
import { SectionMatrix } from "./SectionMatrix"
import { useIbv } from "./IbvProvider"
import {
  applicabilityReason,
  flattenSection,
  getSectionTable,
  isApplicable,
  isGroup,
} from "@/lib/ibv/schema"
import type { FlatRow } from "@/lib/ibv/schema"
import type { Section as SectionModel } from "@/lib/ibv/types"

function Rows({ rows, capValue }: { rows: FlatRow[]; capValue?: boolean }) {
  const { schema, values } = useIbv()
  return (
    <div>
      {rows.map(({ path, field, depth, gates }) => {
        if (!isGroup(field)) {
          return (
            <FieldRow
              key={path}
              field={field}
              path={path}
              depth={depth}
              gates={gates}
              capValue={capValue}
            />
          )
        }
        // A non-applicable node always yields a reason, so it also drives the graying.
        const disabledReason =
          schema && !isApplicable(schema, gates, values)
            ? applicabilityReason(schema, gates, values)
            : null
        return (
          <div
            key={path}
            title={disabledReason ?? undefined}
            className={cn(
              "border-b border-ibv-row bg-ibv-label-bg px-[3px] py-[2px] text-[13.3px] font-bold text-black",
              disabledReason && "opacity-60"
            )}
            style={depth > 0 ? { paddingLeft: 6 + depth * 10 } : undefined}
          >
            {field.title}
          </div>
        )
      })}
    </div>
  )
}

/**
 * A collapsible section: header + (field rows | `ui.layout: "table"` matrix).
 * Table sections render their section-level leaves as plain rows above the
 * matrix. Row-level graying comes from each leaf's own gate chain (which
 * already includes the section's applicable_when). Context sections (known
 * background the voice agent answers from, never asks) get the green header —
 * see UsageLegend.
 */
export function Section({
  sectionKey,
  section,
  defaultOpen = true,
}: {
  sectionKey: string
  section: SectionModel
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const { disputes, flagsFor, resolveOpenDisputes } = useIbv()
  const green = section.role === "context"
  const table = getSectionTable(sectionKey, section)
  const allRows = flattenSection(sectionKey, section)
  // A table section renders only its section-level leaves here; the rest of
  // `allRows` is drawn by the matrix below.
  const rows = table ? table.leaves : allRows

  // This section's own unresolved disputes — the header button resolves exactly
  // this set, never a sibling section's. Taken from `allRows`, not `rows`: a
  // table section's CPT-row cells are nested leaves that `rows` leaves out.
  const pendingPaths = allRows
    .filter((r) => !isGroup(r.field))
    .map((r) => r.path)
    .filter((p) => p in disputes && !flagsFor(p).applied)
  const pendingLabel = `${pendingPaths.length} dispute${pendingPaths.length === 1 ? "" : "s"}`

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "font-ibv",
        // Field-row sections draw the outer frame as the SINGLE source for the
        // top + left/right edges, so the header and rows share one border edge
        // (independent borders rounded to different device pixels at 125%/150%
        // scaling, leaving the header ~1px wider). The bottom edge is closed by
        // the last row's own border-b, so the frame omits `border-b` — otherwise
        // it would double with that row border. Matrix sections are already a
        // single collapsed-border table and keep their own header border.
        !table && "border-x border-t",
        !table && (green ? "border-[#1f9d57]" : "border-ibv-input-border")
      )}
    >
      {/* Positions both the chevron and the resolve pill, which must stay a DOM
          sibling of the trigger — CollapsibleTrigger renders its own <button>. */}
      <div className="relative">
        <CollapsibleTrigger
          className={cn(
            "flex w-full items-center justify-center px-6 py-0.5 text-center text-[13.3px] font-bold text-black",
            // Matrix headers own their full border; field-row headers only need a
            // bottom separator (the section frame supplies the other edges).
            table ? "border" : "border-b",
            green
              ? "border-[#1f9d57] bg-[#22c55e]"
              : "border-ibv-input-border bg-ibv-label-bg"
          )}
        >
          {section.title}
          <ChevronDown
            className={cn(
              "absolute right-2 size-3.5 transition-transform",
              open ? "" : "-rotate-90"
            )}
          />
        </CollapsibleTrigger>
        {pendingPaths.length > 0 && (
          <Tooltip>
            <TooltipTrigger asChild>
              {/* Verb-first label + double-check: a bare ✓+count read as "N values
                  are correct" instead of an action (VR2-162). */}
              <button
                type="button"
                onClick={() => resolveOpenDisputes(pendingPaths)}
                aria-label={`Apply all ${pendingLabel} in ${section.title}`}
                className="absolute right-8 top-1/2 inline-flex -translate-y-1/2 items-center gap-1 whitespace-nowrap rounded-full bg-[#003e64] px-2 py-0.5 text-[11px] font-semibold text-white transition-colors hover:bg-[#002a45]"
              >
                <CheckCheck className="size-3" />
                Apply all · {pendingPaths.length}
              </button>
            </TooltipTrigger>
            <TooltipContent>Apply all {pendingLabel} in this section</TooltipContent>
          </Tooltip>
        )}
      </div>
      <CollapsibleContent>
        {table ? (
          // FieldRow expects an ancestor to supply its left/right edge (it only
          // draws its own borders between/below cells) — the outer frame skips
          // border-x for table sections (see above), so these section-level
          // leaves need their own wrapper for it. The matrix itself doesn't:
          // its <table> already owns a complete collapsed-border frame.
          <div className={cn("border-x", green ? "border-[#1f9d57]" : "border-ibv-input-border")}>
            {/* These leaves span the full matrix width — cap the value cell so the
                control doesn't stretch across the whole table (VR2-162). */}
            <Rows rows={rows} capValue />
          </div>
        ) : (
          <Rows rows={rows} />
        )}
        {table && <SectionMatrix table={table} />}
      </CollapsibleContent>
    </Collapsible>
  )
}
