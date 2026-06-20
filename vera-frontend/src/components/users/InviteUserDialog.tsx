import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { inviteUser, type InviteUserResult } from "@/lib/auth/api"

export function InviteUserDialog({ onInvited }: { onInvited?: () => void } = {}) {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<InviteUserResult | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await inviteUser({ email, name, roleIds: [], sendEmail: true })
      setResult(res)
      onInvited?.()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send invitation.")
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setOpen(false)
    setEmail(""); setName(""); setError(null); setResult(null); setBusy(false)
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : reset())}>
      <DialogTrigger asChild>
        <Button>Invite user</Button>
      </DialogTrigger>
      <DialogContent showCloseButton className="max-w-md gap-0 p-0">
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle className="text-base font-semibold">Invite a user</DialogTitle>
          <DialogDescription>
            They'll get a link to set a password and join this workspace.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <>
            <div className="space-y-4 p-5">
              <p className="text-sm">
                Invitation created for <span className="font-medium">{result.email}</span>
                {result.email_sent ? " and emailed." : "."}
              </p>
              <div className="space-y-1.5">
                <Label htmlFor="invite-url">Invite link</Label>
                <Input
                  id="invite-url"
                  readOnly
                  value={result.invite_url}
                  onFocus={(e) => e.target.select()}
                  className="font-mono text-xs"
                />
              </div>
            </div>
            <div className="flex justify-end border-t border-border p-4">
              <Button onClick={reset} className="min-w-[120px]">Done</Button>
            </div>
          </>
        ) : (
          <form onSubmit={onSubmit}>
            <div className="space-y-4 p-5">
              <div className="space-y-1.5">
                <Label htmlFor="invite-email">Email</Label>
                <Input
                  id="invite-email"
                  type="email"
                  required
                  autoFocus
                  placeholder="person@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="invite-name">Name</Label>
                <Input
                  id="invite-name"
                  placeholder="Jane Doe"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            </div>
            <div className="flex justify-end gap-3 border-t border-border p-4">
              <Button type="button" variant="outline" onClick={reset}>Cancel</Button>
              <Button type="submit" disabled={busy} className="min-w-[120px]">
                {busy ? "Sending…" : "Send invitation"}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
