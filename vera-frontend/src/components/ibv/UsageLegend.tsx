import { allLeaves, fieldUsageOf, sectionEntriesOf, type FieldUsage } from "@/lib/ibv/schema"
import { USAGE_META, USAGE_ORDER } from "./usageMeta"
import type { FormSchema } from "@/lib/ibv/types"
import { cn } from "@/lib/utils"

function Swatch({ className }: { className: string }) {
  return (
    <span
      className={cn("mt-0.5 inline-block size-3.5 shrink-0 rounded-[3px] border", className)}
    />
  )
}

/** One legend row: swatch + bold "Label (count)" + em-dash description. */
function LegendItem({
  swatchClass,
  label,
  count,
  description,
}: {
  swatchClass: string
  label: string
  count: number
  description: string
}) {
  return (
    <p className="flex w-[300px] items-start gap-2">
      <Swatch className={swatchClass} />
      <span>
        <span className="font-semibold">
          {label} ({count})
        </span>{" "}
        — {description}
      </span>
    </p>
  )
}

/**
 * Color-code key, generated from the open form's schema: per-usage field counts
 * plus the green context-section headers. Not a form section — rendered as a
 * full-width strip at the bottom of the form so it never stretches the section
 * layout.
 */
export function UsageLegend({ schema }: { schema: FormSchema }) {
  const counts = new Map<FieldUsage, number>()
  for (const leaf of allLeaves(schema)) {
    const usage = fieldUsageOf(schema, leaf.path, leaf.field)
    counts.set(usage, (counts.get(usage) ?? 0) + 1)
  }
  const contextSections = sectionEntriesOf(schema).filter(
    ([, s]) => s.role === "context"
  ).length

  return (
    <div className="rounded-md border border-ibv-input-border bg-white font-ibv">
      <p className="border-b border-ibv-input-border bg-ibv-label-bg px-6 py-0.5 text-center text-[13.3px] font-bold text-black">
        Color Legend
      </p>
      <div className="flex flex-wrap gap-x-8 gap-y-2 p-2.5 text-[12px] leading-tight text-ibv-label-border">
        {USAGE_ORDER.filter((usage) => (counts.get(usage) ?? 0) > 0).map((usage) => (
          <LegendItem
            key={usage}
            swatchClass={USAGE_META[usage].swatchClass}
            label={USAGE_META[usage].label}
            count={counts.get(usage) ?? 0}
            description={USAGE_META[usage].description}
          />
        ))}
        {contextSections > 0 && (
          <LegendItem
            swatchClass="border-[#1f9d57] bg-[#22c55e]"
            label="Context section"
            count={contextSections}
            description="green headers mark sections whose values the voice agent knows as background instead of asking for them."
          />
        )}
      </div>
    </div>
  )
}
