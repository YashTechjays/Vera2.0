import { useMemo, useState, type JSX } from "react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import type { PlaceholderEntry, PlaceholderGroups } from "@/lib/prompts/document"

type PlaceholderPickerProps = {
  groups: PlaceholderGroups
  onInsert: (token: string) => void
}

function matches(entry: PlaceholderEntry, needle: string): boolean {
  const q = needle.trim().toLowerCase()
  if (q === "") return true
  return entry.token.toLowerCase().includes(q) || entry.detail.toLowerCase().includes(q)
}

function TokenList(props: {
  heading: string
  entries: PlaceholderEntry[]
  onPick: (token: string) => void
}): JSX.Element | null {
  if (props.entries.length === 0) return null
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-muted-foreground">{props.heading}</p>
      {props.entries.map((entry) => (
        <button
          key={entry.token}
          type="button"
          onClick={() => props.onPick(entry.token)}
          className="flex w-full items-baseline justify-between gap-3 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
        >
          <code className="font-mono text-xs">{`{{${entry.token}}}`}</code>
          <span className="truncate text-xs text-muted-foreground">{entry.detail}</span>
        </button>
      ))}
    </div>
  )
}

/** Searchable dialog over the pinned schema's valid placeholder tokens
 *  (system_fields keys + context-leaf paths). Selecting inserts at the caret. */
export function PlaceholderPicker(props: PlaceholderPickerProps): JSX.Element {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const system = useMemo(
    () => props.groups.system.filter((e) => matches(e, query)),
    [props.groups.system, query],
  )
  const context = useMemo(
    () => props.groups.context.filter((e) => matches(e, query)),
    [props.groups.context, query],
  )

  function pick(token: string): void {
    props.onInsert(token)
    setOpen(false)
    setQuery("")
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          Insert placeholder
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[70vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Insert placeholder</DialogTitle>
          <DialogDescription>
            Tokens hydrate per patient form at call time. Valid here: system fields and
            context-role field paths of the published schema. ({"{{value}}"} belongs to
            schema field prompts only, not to session or override text.)
          </DialogDescription>
        </DialogHeader>
        <Input
          autoFocus
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <TokenList heading="System fields" entries={system} onPick={pick} />
        <TokenList heading="Context fields" entries={context} onPick={pick} />
        {system.length === 0 && context.length === 0 && (
          <p className="text-sm text-muted-foreground">No matching placeholders.</p>
        )}
      </DialogContent>
    </Dialog>
  )
}
