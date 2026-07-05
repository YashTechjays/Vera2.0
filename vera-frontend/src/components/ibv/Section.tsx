import { useState } from "react"
import { ChevronDown } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { FieldRow } from "./FieldRow"
import { SectionMatrix } from "./SectionMatrix"
import { useIbv } from "./IbvProvider"
import {
  flattenSection,
  getSectionTable,
  isApplicable,
  isGroup,
} from "@/lib/ibv/schema"
import type { FlatRow } from "@/lib/ibv/schema"
import type { Section as SectionModel } from "@/lib/ibv/types"

function Rows({ rows }: { rows: FlatRow[] }) {
  const { schema, values } = useIbv()
  return (
    <div>
      {rows.map(({ path, field, depth, gates }) =>
        isGroup(field) ? (
          <div
            key={path}
            className={cn(
              "border-b border-ibv-row bg-ibv-label-bg px-[3px] py-[2px] text-[13.3px] font-bold text-black",
              schema && !isApplicable(schema, gates, values) && "opacity-60"
            )}
            style={depth > 0 ? { paddingLeft: 6 + depth * 10 } : undefined}
          >
            {field.title}
          </div>
        ) : (
          <FieldRow
            key={path}
            field={field}
            path={path}
            depth={depth}
            gates={gates}
          />
        )
      )}
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
  const green = section.role === "context"
  const table = getSectionTable(sectionKey, section)
  const rows = table ? table.leaves : flattenSection(sectionKey, section)

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
      <CollapsibleTrigger
        className={cn(
          "relative flex w-full items-center justify-center px-6 py-0.5 text-center text-[13.3px] font-bold text-black",
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
      <CollapsibleContent>
        <Rows rows={rows} />
        {table && <SectionMatrix table={table} />}
      </CollapsibleContent>
    </Collapsible>
  )
}
