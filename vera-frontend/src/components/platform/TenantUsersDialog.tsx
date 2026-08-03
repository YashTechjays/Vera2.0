import { useEffect, useState, type FormEvent } from "react"
import { Check, Copy } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { apiErrorMessage } from "@/lib/api/client"
import {
  inviteTenantUser,
  listTenantRoles,
  listTenantUsers,
  type InviteTenantUserResult,
  type TenantRole,
  type TenantSummary,
  type TenantUser,
} from "@/lib/api/platform"
import { copyText } from "@/lib/clipboard"

type Props = {
  /** null closes the dialog; a tenant opens it for that tenant. */
  tenant: TenantSummary | null
  onClose: () => void
}

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline"> = {
  active: "default",
  invited: "secondary",
  deactivated: "outline",
}

export function TenantUsersDialog({ tenant, onClose }: Props) {
  return (
    <Dialog open={tenant !== null} onOpenChange={(o) => (o ? undefined : onClose())}>
      {/* Mounted per-tenant so every open reloads that tenant's users from scratch. */}
      {tenant && <TenantUsersContent tenant={tenant} onClose={onClose} />}
    </Dialog>
  )
}

type ContentProps = {
  tenant: TenantSummary
  onClose: () => void
}

function TenantUsersContent({ tenant, onClose }: ContentProps) {
  const [users, setUsers] = useState<TenantUser[] | null>(null)
  const [roles, setRoles] = useState<TenantRole[]>([])
  const [error, setError] = useState<string | null>(null)
  const [inviting, setInviting] = useState(false)

  useEffect(() => {
    let cancelled = false
    Promise.all([listTenantUsers(tenant.id), listTenantRoles(tenant.id)])
      .then(([rows, roleRows]) => {
        if (cancelled) return
        setUsers(rows)
        setRoles(roleRows)
      })
      .catch((err) => {
        if (!cancelled) setError(apiErrorMessage(err, "Could not load this tenant's users."))
      })
    return () => {
      cancelled = true
    }
  }, [tenant.id])

  async function reload() {
    try {
      setUsers(await listTenantUsers(tenant.id))
    } catch (err) {
      setError(apiErrorMessage(err, "Could not reload this tenant's users."))
    }
  }

  function inviteSection() {
    if (inviting) {
      return (
        <InviteTenantUserForm
          tenantId={tenant.id}
          roles={roles}
          onDone={() => {
            setInviting(false)
            void reload()
          }}
          onCancel={() => setInviting(false)}
        />
      )
    }
    if (tenant.status !== "active") {
      return (
        <p className="text-sm text-muted-foreground">
          This tenant is deactivated — reactivate it before inviting users.
        </p>
      )
    }
    return (
      <Button type="button" onClick={() => setInviting(true)}>
        Invite user
      </Button>
    )
  }

  return (
    <DialogContent showCloseButton className="max-w-2xl gap-0 p-0">
      <DialogHeader className="border-b border-border p-5 pr-12">
        <DialogTitle className="text-base font-semibold">Users in {tenant.name}</DialogTitle>
        <DialogDescription>
          Invite this client's own users. They sign in at this tenant's login page, not the
          platform one.
        </DialogDescription>
      </DialogHeader>

      <div className="max-h-[60vh] space-y-5 overflow-y-auto p-5">
        {error && (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}

        {users === null && !error && <p className="text-sm text-muted-foreground">Loading…</p>}
        {users?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No users yet — invite the first admin so this tenant can be used.
          </p>
        )}
        {users !== null && users.length > 0 && (
          <ul className="divide-y divide-border rounded-md border border-border">
            {users.map((u) => (
              <li key={u.id} className="flex items-center justify-between gap-3 px-3 py-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{u.name || u.email}</p>
                  <p className="truncate text-xs text-muted-foreground">{u.email}</p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted-foreground">
                    {u.roles.length > 0 ? u.roles.join(", ") : "no role"}
                  </span>
                  <Badge variant={STATUS_VARIANT[u.status] ?? "outline"} className="capitalize">
                    {u.status}
                  </Badge>
                </div>
              </li>
            ))}
          </ul>
        )}

        {inviteSection()}
      </div>

      <div className="flex justify-end border-t border-border p-4">
        <Button variant="outline" onClick={onClose}>
          Close
        </Button>
      </div>
    </DialogContent>
  )
}

type InviteFormProps = {
  tenantId: string
  roles: TenantRole[]
  onDone: () => void
  onCancel: () => void
}

function InviteTenantUserForm({ tenantId, roles, onDone, onCancel }: InviteFormProps) {
  const [email, setEmail] = useState("")
  const [name, setName] = useState("")
  const [roleId, setRoleId] = useState("")
  const [sendEmail, setSendEmail] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<InviteTenantUserResult | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 2000)
    return () => clearTimeout(timer)
  }, [copied])

  async function copyLink() {
    if (!result) return
    setCopied(await copyText(result.invite_url))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      setResult(
        await inviteTenantUser(tenantId, {
          email,
          name,
          roleIds: roleId ? [roleId] : [],
          sendEmail,
        }),
      )
    } catch (err) {
      setError(apiErrorMessage(err, "Could not send the invitation."))
    } finally {
      setBusy(false)
    }
  }

  if (result) {
    return (
      <div className="space-y-3 rounded-md border border-border p-4">
        <p className="text-sm">
          Invitation created for <span className="font-medium">{result.email}</span>
          {result.email_sent ? " and emailed." : "."}
        </p>
        <div className="space-y-1.5">
          <Label htmlFor="tenant-invite-url">Invite link</Label>
          <div className="flex items-center gap-2">
            <Input
              id="tenant-invite-url"
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
            >
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            </Button>
          </div>
        </div>
        <Button type="button" onClick={onDone}>
          Done
        </Button>
      </div>
    )
  }

  const submitLabel = sendEmail ? "Send invitation" : "Create invitation"

  return (
    <form onSubmit={onSubmit} className="space-y-4 rounded-md border border-border p-4">
      <div className="space-y-1.5">
        <Label htmlFor="tenant-invite-email">Email</Label>
        <Input
          id="tenant-invite-email"
          type="email"
          required
          autoFocus
          placeholder="person@client.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="tenant-invite-name">Name</Label>
        <Input
          id="tenant-invite-name"
          placeholder="Jane Doe"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="tenant-invite-role">Role</Label>
        <Select id="tenant-invite-role" value={roleId} onChange={(e) => setRoleId(e.target.value)}>
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
          id="tenant-invite-send-email"
          checked={sendEmail}
          onCheckedChange={(c) => setSendEmail(c === true)}
        />
        <Label htmlFor="tenant-invite-send-email" className="font-normal">
          Send invitation email
        </Label>
      </div>
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-3">
        <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy} className="min-w-[120px]">
          {busy ? "Sending…" : submitLabel}
        </Button>
      </div>
    </form>
  )
}
