import { type JSX } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { PromptTextarea } from "@/components/agent-prompt/PromptTextarea"
import type { OverrideField, OverrideState, PlaceholderGroups } from "@/lib/prompts/document"

type OverrideFieldRowProps = {
  taskKey: string
  field: OverrideField
  label: string
  help: string
  state: OverrideState
  value: string
  defaultText: string | undefined
  errors: string[]
  groups: PlaceholderGroups
  onChange: (text: string) => void
  onOverride: () => void
  onReset: () => void
}

function DefaultBlock(props: { text: string }): JSX.Element {
  return (
    <pre className="rounded-md border bg-muted/50 p-2 font-mono text-xs whitespace-pre-wrap text-muted-foreground">
      {props.text}
    </pre>
  )
}

/** One intro/outro/instructions row with provenance: schema default (read-only,
 *  Override to edit), no-default (Add), or overridden (edit + Reset + the
 *  collapsible default for comparison). Reset REMOVES the override, restoring
 *  the schema default — not the same as blanking it (spec §3.3). */
export function OverrideFieldRow(props: OverrideFieldRowProps): JSX.Element {
  if (props.state === "overridden") {
    return (
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <Badge>Overridden</Badge>
          <Button type="button" variant="ghost" size="sm" onClick={props.onReset}>
            Reset to default
          </Button>
        </div>
        <PromptTextarea
          id={`override-${props.taskKey}-${props.field}`}
          label={props.label}
          help={props.help}
          value={props.value}
          errors={props.errors}
          groups={props.groups}
          onChange={props.onChange}
        />
        {props.defaultText !== undefined && (
          <details className="mt-1">
            <summary className="text-xs text-muted-foreground underline-offset-2 cursor-pointer hover:underline">
              Schema default
            </summary>
            <div className="mt-1">
              <DefaultBlock text={props.defaultText} />
            </div>
          </details>
        )}
      </div>
    )
  }

  const hasDefault = props.state === "default" && props.defaultText !== undefined
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{props.label}</span>
          <Badge variant="secondary">{hasDefault ? "Schema default" : "No default"}</Badge>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={props.onOverride}>
          {hasDefault ? "Override" : "Add"}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">{props.help}</p>
      {hasDefault && props.defaultText !== undefined && <DefaultBlock text={props.defaultText} />}
    </div>
  )
}
