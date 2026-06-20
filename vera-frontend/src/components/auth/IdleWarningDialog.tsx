import { Clock } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

export function IdleWarningDialog({
  open, secondsLeft, onStay, onLogout,
}: {
  open: boolean
  secondsLeft: number
  onStay: () => void
  onLogout: () => void
}) {
  return (
    <Dialog open={open}>
      <DialogContent
        showCloseButton={false}
        onEscapeKeyDown={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
        className="max-w-sm gap-0 p-0"
      >
        <DialogHeader className="p-5">
          <div className="mb-3 flex size-10 items-center justify-center rounded-full bg-muted">
            <Clock className="size-5 text-muted-foreground" />
          </div>
          <DialogTitle className="text-base font-semibold">Still there?</DialogTitle>
          <DialogDescription>
            For your security, you'll be signed out due to inactivity in{" "}
            <span className="font-medium tabular-nums text-foreground">{secondsLeft}s</span>.
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end gap-3 border-t border-border p-4">
          <Button variant="outline" onClick={onLogout}>Log out now</Button>
          <Button onClick={onStay}>Stay signed in</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
