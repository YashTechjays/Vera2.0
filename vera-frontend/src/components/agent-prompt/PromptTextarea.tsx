import { useRef, type JSX } from "react"

import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { PlaceholderPicker } from "@/components/agent-prompt/PlaceholderPicker"
import { insertToken, type PlaceholderGroups } from "@/lib/prompts/document"

type PromptTextareaProps = {
  id: string
  label: string
  help: string
  value: string
  errors: string[]
  groups: PlaceholderGroups
  onChange: (text: string) => void
}

/** Labeled prompt-text editor: help line, placeholder picker wired to the caret,
 *  inline (server- or client-reported) errors. */
export function PromptTextarea(props: PromptTextareaProps): JSX.Element {
  const ref = useRef<HTMLTextAreaElement>(null)

  function handleInsert(token: string): void {
    const caret = ref.current === null ? null : ref.current.selectionStart
    const { next } = insertToken(props.value, token, caret)
    props.onChange(next)
    ref.current?.focus()
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={props.id}>{props.label}</Label>
        <PlaceholderPicker groups={props.groups} onInsert={handleInsert} />
      </div>
      {props.help !== "" && <p className="text-xs text-muted-foreground">{props.help}</p>}
      <Textarea
        id={props.id}
        ref={ref}
        className="min-h-28 font-mono text-xs"
        value={props.value}
        aria-invalid={props.errors.length > 0}
        onChange={(e) => props.onChange(e.target.value)}
      />
      {props.errors.map((message) => (
        <p key={message} className="text-xs text-destructive">
          {message}
        </p>
      ))}
    </div>
  )
}
