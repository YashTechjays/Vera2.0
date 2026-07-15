import { type JSX } from "react"
import { Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"

export type PreviewSection = { label: string; text: string }

type PreviewPaneProps = {
  title: string
  meta: string
  loading: boolean
  error: string | null
  sections: PreviewSection[]
}

/** Dumb renderer of the selection's rendered prompt text — the operator's view
 *  of what the agent actually receives (spec §3.4 decides GET vs POST upstream). */
export function PreviewPane(props: PreviewPaneProps): JSX.Element {
  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{props.title}</h3>
        <p className="text-xs text-muted-foreground">{props.meta}</p>
      </div>
      {props.error !== null && (
        <Alert variant="destructive">
          <AlertDescription>{props.error}</AlertDescription>
        </Alert>
      )}
      {props.loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Rendering…
        </p>
      ) : (
        props.sections.map((section) => (
          <div key={section.label} className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground">{section.label}</p>
            <pre className="max-h-96 overflow-y-auto rounded-md border bg-muted/30 p-2 font-mono text-xs whitespace-pre-wrap">
              {section.text}
            </pre>
          </div>
        ))
      )}
    </div>
  )
}
