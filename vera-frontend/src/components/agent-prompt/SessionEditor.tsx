import { type JSX } from "react"

import { PromptTextarea } from "@/components/agent-prompt/PromptTextarea"
import type { SessionBlock } from "@/lib/api/prompts"
import type { PlaceholderGroups } from "@/lib/prompts/document"

type SessionEditorProps = {
  session: SessionBlock
  errors: Record<string, string[]>
  groups: PlaceholderGroups
  onChange: (field: keyof SessionBlock, text: string) => void
}

// Help texts mirror the SessionBlock Field(description=…) intents in
// vera_core/forms/prompting.py — the meaning lives where the model lives.
const SESSION_FIELDS: { field: keyof SessionBlock; label: string; help: string }[] = [
  {
    field: "persona",
    label: "Persona",
    help:
      "Who the agent is: name (VERA), voice and temperament, speech pacing habits, " +
      "how it refers to itself, pronunciation tendencies.",
  },
  {
    field: "goal",
    label: "Goal",
    help:
      "What the call is for — the north star the agent falls back on when the " +
      "conversation drifts.",
  },
  {
    field: "base_instructions",
    label: "Base instructions",
    help:
      "Global behavior rules applied across every task: turn-taking discipline, " +
      "value-recording rules, hold handling, role enforcement, anti-repetition.",
  },
]

/** The literal session block — no default/override concept here; what you see
 *  is what ships (spec §3.2). All three fields are required. */
export function SessionEditor(props: SessionEditorProps): JSX.Element {
  return (
    <div className="space-y-4">
      {SESSION_FIELDS.map(({ field, label, help }) => (
        <PromptTextarea
          key={field}
          id={`session-${field}`}
          label={label}
          help={help}
          value={props.session[field]}
          errors={props.errors[`session.${field}`] ?? []}
          groups={props.groups}
          onChange={(text) => props.onChange(field, text)}
        />
      ))}
    </div>
  )
}
