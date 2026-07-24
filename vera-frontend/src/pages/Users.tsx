import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { InviteUserDialog } from "@/components/users/InviteUserDialog"
import { ApiError } from "@/lib/api/client"
import { deactivateUser, listUsers, type UserSummary } from "@/lib/auth/api"
import { usePermission } from "@/lib/auth/permissions"

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline"> = {
  active: "default",
  invited: "secondary",
  deactivated: "outline",
}

function roleLabel(name: string): string {
  return name
    .split("_")
    .map((w) => w.charAt(0) + w.slice(1).toLowerCase())
    .join(" ")
}

function formatDate(iso: string | null): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
}

export function Users() {
  const canRead = usePermission("users:read")
  const canManage = usePermission("users:manage")
  const [users, setUsers] = useState<UserSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Deactivation confirmation popup state.
  const [pending, setPending] = useState<UserSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [dialogError, setDialogError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setUsers(await listUsers())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load users.")
    }
  }, [])

  // Initial load. setState only in the async callbacks (not synchronously in the
  // effect body), with a cancelled flag to avoid a post-unmount update.
  useEffect(() => {
    if (!canRead) return
    let cancelled = false
    listUsers()
      .then((u) => {
        if (!cancelled) setUsers(u)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load users.")
      })
    return () => {
      cancelled = true
    }
  }, [canRead])

  // Auto-dismiss the success notice after a few seconds.
  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 5000)
    return () => clearTimeout(timer)
  }, [notice])

  function askDeactivate(user: UserSummary) {
    setNotice(null)
    setDialogError(null)
    setPending(user)
  }

  function closeDialog() {
    if (busy) return
    setPending(null)
    setDialogError(null)
  }

  async function confirmDeactivate() {
    if (!pending) return
    const label = pending.name || pending.email
    setBusy(true)
    setDialogError(null)
    try {
      await deactivateUser(pending.id)
      setPending(null)
      await load()
      setNotice(`${label} has been deactivated.`)
    } catch (err) {
      setDialogError(err instanceof ApiError ? err.message : "Could not deactivate user.")
    } finally {
      setBusy(false)
    }
  }

  if (!canRead) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Users</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          You don't have permission to view users.
        </p>
      </div>
    )
  }

  const counts = {
    active: users?.filter((u) => u.status === "active").length ?? 0,
    invited: users?.filter((u) => u.status === "invited").length ?? 0,
    deactivated: users?.filter((u) => u.status === "deactivated").length ?? 0,
  }
  const colSpan = canManage ? 6 : 5

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Users</h1>
          <p className="text-sm text-muted-foreground">
            {users
              ? `${counts.active} active · ${counts.invited} invited · ${counts.deactivated} deactivated`
              : "Loading…"}
          </p>
        </div>
        {canManage && <InviteUserDialog onInvited={load} />}
      </div>

      {notice && (
        <div className="flex items-center gap-2" role="status">
          <p className="text-sm text-emerald-600 dark:text-emerald-400">{notice}</p>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() => setNotice(null)}
            className="text-sm leading-none text-emerald-600 hover:text-emerald-800 dark:text-emerald-400 dark:hover:text-emerald-200"
          >
            ×
          </button>
        </div>
      )}
      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Last login</TableHead>
              {canManage && <TableHead>Actions</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {users === null && (
              <TableRow>
                <TableCell colSpan={colSpan} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {users?.length === 0 && (
              <TableRow>
                <TableCell colSpan={colSpan} className="py-6 text-center text-muted-foreground">
                  No users yet.
                </TableCell>
              </TableRow>
            )}
            {users?.map((u) => (
              <TableRow key={u.id}>
                <TableCell className="font-medium">{u.name || "—"}</TableCell>
                <TableCell>{u.email}</TableCell>
                <TableCell>
                  {u.roles.length > 0 ? u.roles.map(roleLabel).join(", ") : "—"}
                </TableCell>
                <TableCell>
                  <Badge variant={STATUS_VARIANT[u.status] ?? "outline"} className="capitalize">
                    {u.status}
                  </Badge>
                </TableCell>
                <TableCell className="text-muted-foreground">{formatDate(u.last_login_at)}</TableCell>
                {canManage && (
                  <TableCell>
                    {u.status !== "deactivated" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        // The ghost button has px-2.5 (10px) inner padding and the
                        // TableCell has p-2 (8px). -ml-2 offsets the cell padding so
                        // the button label aligns with the "Actions" TableHead text
                        // (which also has px-2). Previously -ml-2.5 over-shot by 2px.
                        className="-ml-2 text-destructive hover:text-destructive"
                        onClick={() => askDeactivate(u)}
                      >
                        Deactivate
                      </Button>
                    )}
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Deactivation confirmation popup */}
      <Dialog open={pending !== null} onOpenChange={(o) => (o ? undefined : closeDialog())}>
        <DialogContent showCloseButton={!busy} className="max-w-sm gap-0 p-0">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">Deactivate user?</DialogTitle>
            <DialogDescription>
              {pending && (
                <>
                  <span className="font-medium text-foreground">{pending.name || pending.email}</span>{" "}
                  will lose access immediately and won't be able to sign in.
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          {dialogError && (
            <p className="px-5 pb-1 text-sm text-destructive" role="alert">{dialogError}</p>
          )}
          <div className="flex justify-end gap-3 border-t border-border p-4">
            <Button variant="outline" onClick={closeDialog} disabled={busy}>Cancel</Button>
            <Button
              variant="destructive"
              onClick={confirmDeactivate}
              disabled={busy}
              className="min-w-[120px]"
            >
              {busy ? "Deactivating…" : "Deactivate"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
