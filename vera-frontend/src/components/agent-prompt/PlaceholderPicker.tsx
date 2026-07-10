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
  detailLabel: string
  entries: PlaceholderEntry[]
  onPick: (token: string) => void
}): JSX.Element | null {
  if (props.entries.length === 0) return null
  return (
    <div>
      <div className="flex items-baseline justify-between px-2 pt-2 pb-1">
        <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {props.heading}
        </p>
        <p className="text-[10px] text-muted-foreground/70">{props.detailLabel}</p>
      </div>
      {props.entries.map((entry) => (
        <button
          key={entry.token}
          type="button"
          onClick={() => props.onPick(entry.token)}
          className="flex w-full items-center justify-between gap-4 rounded-md px-2 py-1.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
        >
          <code className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
            {`{{${entry.token}}}`}
          </code>
          <span className="truncate font-mono text-[11px] text-muted-foreground">
            {entry.detail}
          </span>
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

  function handleOpenChange(nextOpen: boolean): void {
    setOpen(nextOpen)
    if (!nextOpen) setQuery("")
  }

  function pick(token: string): void {
    props.onInsert(token)
    handleOpenChange(false)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          Insert placeholder
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-w-lg gap-0 p-0"
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle>Insert placeholder</DialogTitle>
          <DialogDescription>
            Tokens hydrate per patient form at call time. Inserted at your cursor.
          </DialogDescription>
        </DialogHeader>
        <div className="border-b border-border p-3">
          <Input
            autoFocus
            placeholder="Search tokens and fields…"
            aria-label="Search placeholders"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="max-h-[45vh] space-y-2 overflow-y-auto p-2">
          <TokenList
            heading="System fields"
            detailLabel="mapped field path"
            entries={system}
            onPick={pick}
          />
          <TokenList
            heading="Context fields"
            detailLabel="field title"
            entries={context}
            onPick={pick}
          />
          {system.length === 0 && context.length === 0 && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              No matching placeholders.
            </p>
          )}
        </div>
        <p className="border-t border-border px-5 py-3 text-xs text-muted-foreground">
          {"{{value}}"} belongs to schema field prompts only — it is not valid in session or
          override text.
        </p>
      </DialogContent>
    </Dialog>
  )
}
