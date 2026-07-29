import { type JSX } from "react"

import { OverrideFieldRow } from "@/components/agent-prompt/OverrideFieldRow"
import type { PromptDocument } from "@/lib/api/prompts"
import {
  overrideStateOf,
  type OverrideField,
  type PlaceholderGroups,
  type TaskDefaults,
} from "@/lib/prompts/document"

type TaskOverrideEditorProps = {
  task: TaskDefaults
  document: PromptDocument
  errors: Record<string, string[]>
  groups: PlaceholderGroups
  onSet: (field: OverrideField, text: string) => void
  onClear: (field: OverrideField) => void
}

const TASK_FIELDS: { field: OverrideField; label: string; help: string }[] = [
  {
    field: "intro",
    label: "Intro",
    help: "Spoken verbatim when the task starts. Leave it blank to say nothing on entry.",
  },
  {
    field: "outro",
    label: "Outro",
    help: "Spoken verbatim when the task completes. Leave it blank to say nothing on exit.",
  },
  {
    field: "prompt",
    label: "Instructions",
    help:
      "Leads the compiled task prompt; schema-derived questions and rules are " +
      "appended after it.",
  },
]

/** Effective text per field = override ?? schema default; editing creates the
 *  override, Reset removes it (spec §3.3). */
export function TaskOverrideEditor(props: TaskOverrideEditorProps): JSX.Element {
  return (
    <div className="space-y-6">
      {TASK_FIELDS.map(({ field, label, help }) => {
        const defaultText = props.task[field]
        const override = props.document.task_overrides[props.task.task_key]?.[field]
        return (
          <OverrideFieldRow
            key={field}
            taskKey={props.task.task_key}
            field={field}
            label={label}
            help={help}
            state={overrideStateOf(props.document, props.task.task_key, field, defaultText)}
            value={typeof override === "string" ? override : ""}
            defaultText={defaultText}
            errors={props.errors[`task_overrides.${props.task.task_key}.${field}`] ?? []}
            groups={props.groups}
            onChange={(text) => props.onSet(field, text)}
            onOverride={() => props.onSet(field, defaultText ?? "")}
            onReset={() => props.onClear(field)}
          />
        )
      })}
    </div>
  )
}
