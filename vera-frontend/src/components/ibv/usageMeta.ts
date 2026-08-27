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
  // Pink is the only hue in this form that no SEVERITY claims, which is the point: every warm
  // tone here already encodes confidence — yellow #FEFCE8 medium, amber #FFFBEB low, red
  // #FEF2F2 very-low (`confidenceHighlightClass`) — and amber again on the Unverified pill.
  // A per-call field is a CATEGORY, not a severity, so it must not borrow a severity's colour.
  // Green (context + high confidence), violet (system), teal (#d0e0e3 value cells), blue
  // (default highlight) and gray (UI-only) are likewise spoken for.
  perCall: {
    label: "Per-call field",
    description:
      "Asked on every call. Its value describes THAT call — the representative's name, the call reference number — so it has no form-level baseline to diverge from and never raises a dispute.",
    labelCellClass: "bg-pink-100",
    swatchClass: "border-pink-300 bg-pink-100",
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
export const USAGE_ORDER: FieldUsage[] = ["system", "context", "asked", "perCall", "noop"]
