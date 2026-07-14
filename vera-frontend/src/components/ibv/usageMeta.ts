import type { FieldUsage } from "@/lib/ibv/schema"

/**
 * One place for the field-usage color coding: FieldRow tints its label cell
 * with `labelCellClass`, and UsageLegend (below the reference rail) explains
 * the same classes.
 */
export const USAGE_META: Record<
  FieldUsage,
  { label: string; description: string; labelCellClass: string; swatchClass: string }
> = {
  // Amber: distinct from violet (system), green (context), and the dispute red.
  // Color is easily adjustable — change labelCellClass/swatchClass here if product
  // requests a different hue.
  prerequisite: {
    label: "Prerequisite",
    description:
      "Required before the call begins — the voice agent uses these to set up and route the call (appointment date, appointment type, callback number).",
    labelCellClass: "bg-amber-100",
    swatchClass: "border-amber-400 bg-amber-100",
  },
  // Violet, not red: the dispute highlight already uses red for low-confidence
  // captures, so a red system tint would read as a dispute.
  system: {
    label: "System field",
    description:
      "Required by the platform — worklists, integrations and call setup read these; their values are also given to the voice agent as known context.",
    labelCellClass: "bg-violet-100",
    swatchClass: "border-violet-300 bg-violet-100",
  },
  context: {
    label: "Voice-agent context",
    description:
      "Fed to the voice agent as known background — answered if the representative asks, never volunteered, never asked.",
    labelCellClass: "bg-green-100",
    swatchClass: "border-green-400 bg-green-100",
  },
  asked: {
    label: "Collected on the call",
    description: "Asked (or confirmed) by the voice agent during the verification call.",
    labelCellClass: "",
    swatchClass: "border-ibv-input-border bg-ibv-input-bg",
  },
  // Diagonal gray hatching ("not in use"), not another hue — flat gray was
  // indistinguishable from the default label cells.
  noop: {
    label: "UI only",
    description:
      "Display / data entry only (including UI-only sections) — not asked on the call and not part of the agent's context.",
    labelCellClass:
      "bg-[repeating-linear-gradient(45deg,#e4e4e7_0px,#e4e4e7_5px,#fafafa_5px,#fafafa_10px)]",
    swatchClass:
      "border-zinc-400 bg-[repeating-linear-gradient(45deg,#e4e4e7_0px,#e4e4e7_3px,#fafafa_3px,#fafafa_6px)]",
  },
}

/** Legend display order. */
export const USAGE_ORDER: FieldUsage[] = ["prerequisite", "system", "context", "asked", "noop"]
