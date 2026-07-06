import { useEffect, useState, type FormEvent } from "react"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { ApiError } from "@/lib/api/client"
import { inviteUser, listRoles, type InviteUserResult, type RoleSummary } from "@/lib/auth/api"

export function InviteUserDialog({ onInvited }: { onInvited?: () => void } = {}) {
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [sendEmail, setSendEmail] = useState(true)
  const [roles, setRoles] = useState<RoleSummary[]>([])
  const [roleId, setRoleId] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<InviteUserResult | null>(null)
  const [copied, setCopied] = useState(false)

  // Load the assignable roles (global system roles + this tenant's custom roles)
  // each time the dialog opens, so the picker reflects any role created since the
  // last time it was shown.
  useEffect(() => {
    if (!open) return
    let cancelled = false
    listRoles()
      .then((r) => {
        if (!cancelled) setRoles(r)
      })
      .catch(() => {
        // Non-fatal: the invite still works with no role selected.
      })
    return () => {
      cancelled = true
    }
  }, [open])

  function copyLink() {
    if (!result) return
    void navigator.clipboard.writeText(result.invite_url).then(() => setCopied(true))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await inviteUser({
        email,
        name,
        roleIds: roleId ? [roleId] : [],
        sendEmail,
      })
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
    setEmail(""); setName(""); setSendEmail(true); setRoleId(""); setError(null); setResult(null); setBusy(false); setCopied(false)
  }

  const submitLabel = sendEmail ? "Send invitation" : "Create invitation"
  const submitBusyLabel = sendEmail ? "Sending…" : "Creating…"

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
                <div className="flex items-center gap-2">
                  <Input
                    id="invite-url"
                    readOnly
                    value={result.invite_url}
                    onFocus={(e) => e.target.select()}
                    className="font-mono text-xs"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={copyLink}
                    aria-label={copied ? "Link copied" : "Copy invite link"}
                    title={copied ? "Copied" : "Copy"}
                  >
                    {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
                  </Button>
                </div>
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
              <div className="space-y-1.5">
                <Label htmlFor="invite-role">Role</Label>
                <Select
                  id="invite-role"
                  value={roleId}
                  onChange={(e) => setRoleId(e.target.value)}
                >
                  <option value="">No role (invite only)</option>
                  {roles.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </Select>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <Checkbox
                  id="invite-send-email"
                  checked={sendEmail}
                  onCheckedChange={(checked) => setSendEmail(checked === true)}
                />
                <Label htmlFor="invite-send-email" className="font-normal">
                  Send invitation email
                </Label>
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            </div>
            <div className="flex justify-end gap-3 border-t border-border p-4">
              <Button type="button" variant="outline" onClick={reset}>Cancel</Button>
              <Button type="submit" disabled={busy} className="min-w-[120px]">
                {busy ? submitBusyLabel : submitLabel}
              </Button>
            </div>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
