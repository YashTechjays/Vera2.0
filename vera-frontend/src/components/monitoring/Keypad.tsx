import { useState } from "react"
import { Phone } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]

/** DTMF keypad (ported from smart-caller-fe, restyled to our scheme). */
export function Keypad({
  open,
  onOpenChange,
  onSend,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onSend?: (digits: string) => void
}) {
  const [digits, setDigits] = useState("")

  const press = (k: string) => setDigits((d) => (d + k).slice(-16))
  const clear = () => setDigits("")
  const enter = () => {
    if (digits.trim()) {
      onSend?.(digits)
      setDigits("")
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton className="w-[320px] gap-0 p-0">
        <DialogHeader className="flex-row items-center gap-2 space-y-0 border-b border-border p-4">
          <Phone className="size-4 text-foreground" />
          <DialogTitle className="text-base">Keypad</DialogTitle>
        </DialogHeader>

        <div className="p-5">
          {/* Display */}
          <div className="mb-4 flex min-h-[50px] items-center justify-center rounded-md border border-border bg-muted px-3 text-xl font-semibold tracking-[0.1em] tabular-nums text-foreground">
            {digits || <span className="text-muted-foreground/50">—</span>}
          </div>

          {/* Keys */}
          <div className="grid grid-cols-3 gap-3">
            {KEYS.map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => press(k)}
                className="mx-auto flex size-16 items-center justify-center rounded-full border border-border bg-muted text-lg font-semibold text-foreground transition-colors hover:bg-foreground hover:text-background"
              >
                {k}
              </button>
            ))}
          </div>

          {/* Actions */}
          <div className="mt-4 flex gap-2">
            <Button variant="outline" className="flex-1" onClick={clear}>
              Clear
            </Button>
            <Button className="flex-1" onClick={enter}>
              Enter
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
