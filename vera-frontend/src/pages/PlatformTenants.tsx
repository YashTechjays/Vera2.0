import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { TenantFormDialog } from "@/components/platform/TenantFormDialog"
import { TenantUsersDialog } from "@/components/platform/TenantUsersDialog"
import { apiErrorMessage } from "@/lib/api/client"
import {
  deactivateTenant,
  getTenant,
  listTenants,
  reactivateTenant,
  type TenantDetail,
  type TenantSummary,
} from "@/lib/api/platform"
import { selectIsSuperAdmin } from "@/store/authSlice"
import { useAppSelector } from "@/store/hooks"

const STATUS_VARIANT: Record<string, "default" | "outline"> = {
  active: "default",
  deactivated: "outline",
}

export function PlatformTenants() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [tenants, setTenants] = useState<TenantSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<TenantDetail | null>(null)
  const [openingId, setOpeningId] = useState<string | null>(null)
  const [viewingUsersOf, setViewingUsersOf] = useState<TenantSummary | null>(null)

  const [pending, setPending] = useState<TenantSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [dialogError, setDialogError] = useState<string | null>(null)

  // status: "all" — the management table must list deactivated tenants too, which
  // the active-only default (kept for the elevation picker) would hide.
  const load = useCallback(async () => {
    setError(null)
    try {
      setTenants(await listTenants({ status: "all" }))
    } catch (err) {
      setError(apiErrorMessage(err, "Could not load tenants."))
    }
  }, [])

  // Initial load. setState only in the async callbacks (not synchronously in the
  // effect body), with a cancelled flag to avoid a post-unmount update.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listTenants({ status: "all" })
      .then((t) => {
        if (!cancelled) setTenants(t)
      })
      .catch((err) => {
        if (!cancelled) setError(apiErrorMessage(err, "Could not load tenants."))
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(null), 5000)
    return () => clearTimeout(timer)
  }, [notice])

  // Platform-only surface; the backend also enforces this, but hide it cleanly.
  if (!isSuperAdmin) {
    return (
      <p className="text-sm text-muted-foreground">
        This page is only available to platform operators.
      </p>
    )
  }

  const activeCount = tenants?.filter((t) => t.status === "active").length ?? 0
  const deactivating = pending?.status === "active"
  const flipLabel = deactivating ? "Deactivate" : "Reactivate"

  function openCreate() {
    setNotice(null)
    setEditing(null)
    setFormOpen(true)
  }

  // The list withholds manage-only fields, so the edit form needs the full detail.
  async function openEdit(row: TenantSummary) {
    setNotice(null)
    setError(null)
    setOpeningId(row.id)
    try {
      setEditing(await getTenant(row.id))
      setFormOpen(true)
    } catch (err) {
      setError(apiErrorMessage(err, "Could not open that tenant."))
    } finally {
      setOpeningId(null)
    }
  }

  function askFlip(row: TenantSummary) {
    setNotice(null)
    setDialogError(null)
    setPending(row)
  }

  function closeDialog() {
    if (busy) return
    setPending(null)
    setDialogError(null)
  }

  async function confirmFlip() {
    if (!pending) return
    const { id, name } = pending
    setBusy(true)
    setDialogError(null)
    try {
      await (deactivating ? deactivateTenant(id) : reactivateTenant(id))
      setPending(null)
      await load()
      setNotice(`${name} has been ${deactivating ? "deactivated" : "reactivated"}.`)
    } catch (err) {
      setDialogError(apiErrorMessage(err, "Could not change the tenant status."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Tenants</h1>
          <p className="text-sm text-muted-foreground">
            {tenants ? `${activeCount} active of ${tenants.length}` : "Loading…"} — client
            organisations. Invite each tenant's users after creating it.
          </p>
        </div>
        <Button onClick={openCreate}>New tenant</Button>
      </div>

      {notice && (
        <p className="text-sm text-emerald-600 dark:text-emerald-400" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Slug</TableHead>
              <TableHead>Region</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tenants === null && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {tenants?.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  No tenants yet.
                </TableCell>
              </TableRow>
            )}
            {tenants?.map((t) => {
              const isActive = t.status === "active"
              return (
                <TableRow key={t.id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell className="font-mono text-xs">{t.slug}</TableCell>
                  <TableCell>{t.region || "—"}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[t.status] ?? "outline"} className="capitalize">
                      {t.status}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="-ml-2 mr-1"
                      onClick={() => openEdit(t)}
                      disabled={openingId === t.id}
                    >
                      {openingId === t.id ? "Opening…" : "Edit"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="mr-1"
                      onClick={() => setViewingUsersOf(t)}
                    >
                      Users
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className={isActive ? "text-destructive hover:text-destructive" : ""}
                      onClick={() => askFlip(t)}
                    >
                      {isActive ? "Deactivate" : "Reactivate"}
                    </Button>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <TenantFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        tenant={editing}
        onSaved={() => {
          void load()
          setNotice(editing ? "Tenant updated." : "Tenant created.")
        }}
      />

      <TenantUsersDialog tenant={viewingUsersOf} onClose={() => setViewingUsersOf(null)} />

      <Dialog open={pending !== null} onOpenChange={(o) => (o ? undefined : closeDialog())}>
        <DialogContent showCloseButton={!busy} className="max-w-sm gap-0 p-0">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">{flipLabel} tenant?</DialogTitle>
            <DialogDescription>
              {pending && (
                <>
                  <span className="font-medium text-foreground">{pending.name}</span>{" "}
                  {deactivating
                    ? "will stop accepting logins. Anyone already signed in keeps their session until it expires."
                    : "will accept logins again."}
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          {dialogError && (
            <p className="px-5 pb-1 text-sm text-destructive" role="alert">
              {dialogError}
            </p>
          )}
          <div className="flex justify-end gap-3 border-t border-border p-4">
            <Button variant="outline" onClick={closeDialog} disabled={busy}>
              Cancel
            </Button>
            <Button
              variant={deactivating ? "destructive" : "default"}
              onClick={confirmFlip}
              disabled={busy}
              className="min-w-[120px]"
            >
              {busy ? "Saving…" : flipLabel}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
