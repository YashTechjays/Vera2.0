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
import { flattenSection, getSectionMatrix } from "@/lib/ibv/schema"
import type { IbvSection } from "@/lib/ibv/types"

/** A collapsible section: header + (field rows | CPT matrix). */
export function Section({
  section,
  defaultOpen = true,
  green = false,
}: {
  section: IbvSection
  defaultOpen?: boolean
  /** reference-rail style: bright green header bar (vs. the gray legend) */
  green?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const matrix = getSectionMatrix(section)
  const rows = matrix ? [] : flattenSection(section)

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
        !matrix && "border-x border-t",
        !matrix && (green ? "border-[#1f9d57]" : "border-ibv-input-border")
      )}
    >
      <CollapsibleTrigger
        className={cn(
          "relative flex w-full items-center justify-center px-6 py-0.5 text-center text-[13.3px] font-bold text-black",
          // Matrix headers own their full border; field-row headers only need a
          // bottom separator (the section frame supplies the other edges).
          matrix ? "border" : "border-b",
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
        {matrix ? (
          <SectionMatrix matrix={matrix} />
        ) : (
          <div>
            {rows.map(({ path, field, depth }) =>
              field.type === "object" ? (
                <div
                  key={path}
                  className="border-b border-ibv-row bg-ibv-label-bg px-[3px] py-[2px] text-[13.3px] font-bold text-black"
                  style={depth > 0 ? { paddingLeft: 6 + depth * 10 } : undefined}
                >
                  {field.title}
                </div>
              ) : (
                <FieldRow key={path} field={field} path={path} depth={depth} />
              )
            )}
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}
