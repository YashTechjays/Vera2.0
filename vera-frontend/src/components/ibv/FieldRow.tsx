import { cn } from "@/lib/utils"
import { useIbv } from "./IbvProvider"
import { FieldRenderer } from "./FieldRenderer"
import {
  ApplyButton,
  SwapButton,
  DisputeBadge,
} from "./DisputeControls"
import { badgeValue, confidenceHighlightClass } from "@/lib/ibv/disputes"
import type { IbvField } from "@/lib/ibv/types"

type Props = {
  field: IbvField
  path: string
  depth: number
}

/**
 * One dense spreadsheet row (smart-caller-fe `.field-group`): ~180px gray label
 * cell (navy borders) + pale-blue input cell (teal borders) + inline disputes.
 */
export function FieldRow({ field, path, depth }: Props) {
  const {
    values,
    setValue,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
  } = useIbv()

  const value = values[path] ?? ""
  const dispute = disputeFor(path)
  const flags = flagsFor(path)
  const required = field.required_state === "required"

  // Highlight + badge only while an unresolved dispute is present.
  const showDispute = !!dispute && !flags.applied
  const highlightClass = showDispute
    ? confidenceHighlightClass(dispute!.confidence)
    : undefined

  return (
    <div className="flex min-h-[26px]">
      <div
        className={cn(
          "flex w-[210px] min-w-[210px] shrink-0 items-center gap-1 border-r border-b border-ibv-label-border bg-white px-1.5 py-1 text-left font-ibv text-[13.3px] font-semibold text-ibv-label-border"
        )}
        style={depth > 0 ? { paddingLeft: 6 + depth * 10 } : undefined}
      >
        <span className="min-w-0 flex-1 leading-tight break-words">
          {field.title}
        </span>
        {required && (
          <span className="flex shrink-0 items-center gap-1">
            <span className="text-[#b91c1c]">*</span>
          </span>
        )}
      </div>

      <div className="relative flex flex-1 items-stretch">
        <FieldRenderer
          field={field}
          path={path}
          value={value}
          onChange={(v) => setValue(path, v)}
          highlightClass={highlightClass}
          inputPaddingRight={showDispute ? "150px" : undefined}
          noRightBorder
        />
        {showDispute && (
          <div className="absolute top-1/2 right-1.5 flex -translate-y-1/2 items-center gap-1">
            <SwapButton swapped={flags.swapped} onClick={() => swapDispute(path)} />
            <ApplyButton applied={flags.applied} onClick={() => applyDispute(path)} />
            <DisputeBadge
              value={badgeValue(dispute!, flags)}
              dispute={dispute!}
              label={flags.swapped ? "Captured" : "Prior"}
            />
          </div>
        )}
      </div>
    </div>
  )
}
