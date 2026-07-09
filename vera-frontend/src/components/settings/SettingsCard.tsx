import { useState, type ReactNode } from "react"
import { ChevronDown } from "lucide-react"

import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { cn } from "@/lib/utils"

type SettingsCardProps = {
  title: string
  description: string
  /** Optional primary action rendered in the header's top-right corner. */
  action?: ReactNode
  /** Start expanded. Defaults to collapsed — the page is a scannable list of sections. */
  defaultOpen?: boolean
  children: ReactNode
}

/** The one section shell for the Settings page: a card whose header is a
 *  click-to-expand toggle, so the page stays a compact, scannable list and
 *  only the section being worked on takes up space. */
export function SettingsCard({
  title,
  description,
  action,
  defaultOpen = false,
  children,
}: SettingsCardProps) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <Card>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger asChild>
          <CardHeader
            className={cn(
              "cursor-pointer select-none transition-colors hover:bg-muted/40",
              // The Card's own padding renders above/below the header; only
              // draw the divider when content is showing beneath it.
              open && "border-b pb-(--card-spacing)",
            )}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              // Only toggle for keys pressed on the header itself — Enter/Space on
              // the action button (a child) must not also fold the section.
              if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault()
                setOpen((cur) => !cur)
              }
            }}
          >
            <CardTitle className="flex items-center gap-2">
              <ChevronDown
                className={cn("size-4 text-muted-foreground transition-transform", !open && "-rotate-90")}
              />
              {title}
            </CardTitle>
            <CardDescription className="pl-6">{description}</CardDescription>
            {action && (
              <CardAction onClick={(e) => e.stopPropagation()}>{action}</CardAction>
            )}
          </CardHeader>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CardContent className="space-y-3 pt-(--card-spacing)">{children}</CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  )
}
