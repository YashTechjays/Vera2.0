import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { ProviderFormDialog } from "@/components/insurance-providers/ProviderFormDialog"
import { ApiError } from "@/lib/api/client"
import {
  deleteProvider, listProviders, type ProviderStatus, type ProviderSummary,
} from "@/lib/api/insuranceProviders"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

const STATUS_VARIANT: Record<ProviderStatus, "default" | "outline"> = {
  active: "default",
  inactive: "outline",
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
}

/** "HH:MM–HH:MM" when both are set, or one bound with a dash on the missing side
 *  ("09:00–" / "–17:00") so a lone value never reads as the wrong bound; "—" when neither. */
function formatHours(start: string | null, end: string | null): string {
  const s = start?.slice(0, 5)
  const e = end?.slice(0, 5)
  if (!s && !e) return "—"
  return `${s ?? ""}–${e ?? ""}`
}

export function InsuranceProviders() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [providers, setProviders] = useState<ProviderSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Create/edit dialog: editing === null means "create".
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<ProviderSummary | null>(null)

  // Soft-delete confirmation.
  const [pending, setPending] = useState<ProviderSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [dialogError, setDialogError] = useState<string | null>(null)

  // Refresh after a mutation. On error, drop out of the initial loading state (setProviders
  // to [] only if still null) so the table never sticks on "Loading…" under the error banner,
  // while an existing list survives a failed refresh.
  const load = useCallback(async () => {
    setError(null)
    try {
      setProviders(await listProviders())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load providers.")
      setProviders((prev) => prev ?? [])
    }
  }, [])

  // Initial load. setState only in the async callbacks (never synchronously in the effect body,
  // per react-hooks/set-state-in-effect), with a cancelled flag to avoid a post-unmount update —
  // the same pattern as Users.tsx / IvrPlaybooks.tsx.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listProviders()
      .then((p) => {
        if (!cancelled) setProviders(p)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : "Could not load providers.")
        setProviders((prev) => prev ?? [])  // else the table sticks on "Loading…"
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  function openCreate() {
    setEditing(null)
    setFormOpen(true)
  }

  function openEdit(provider: ProviderSummary) {
    setEditing(provider)
    setFormOpen(true)
  }

  function askDelete(provider: ProviderSummary) {
    setDialogError(null)
    setPending(provider)
  }

  function closeDelete() {
    if (busy) return
    setPending(null)
    setDialogError(null)
  }

  async function confirmDelete() {
    if (!pending) return
    setBusy(true)
    setDialogError(null)
    try {
      await deleteProvider(pending.id)
      setPending(null)
      await load()
    } catch (err) {
      setDialogError(err instanceof ApiError ? err.message : "Could not deactivate provider.")
    } finally {
      setBusy(false)
    }
  }

  if (!isSuperAdmin) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Insurance Providers</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          This catalog is managed by platform operators only.
        </p>
      </div>
    )
  }

  const activeCount = providers?.filter((p) => p.status === "active").length ?? 0

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Insurance Providers</h1>
          <p className="text-sm text-muted-foreground">
            {providers ? `${activeCount} active · ${providers.length} total` : "Loading…"}
          </p>
        </div>
        <Button onClick={openCreate}>New provider</Button>
      </div>

      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Working hours</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {providers === null && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {providers?.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  No insurance providers yet.
                </TableCell>
              </TableRow>
            )}
            {providers?.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-medium">{p.name}</TableCell>
                <TableCell className="text-muted-foreground">
                  {formatHours(p.working_hour_start, p.working_hour_end)}
                </TableCell>
                <TableCell>
                  <Badge variant={STATUS_VARIANT[p.status]} className="capitalize">
                    {p.status}
                  </Badge>
                </TableCell>
                <TableCell className="text-muted-foreground">{formatDate(p.created_at)}</TableCell>
                <TableCell>
                  <div className="-ml-2.5 flex gap-1">
                    <Button variant="ghost" size="sm" onClick={() => openEdit(p)}>
                      Edit
                    </Button>
                    {p.status === "active" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:text-destructive"
                        onClick={() => askDelete(p)}
                      >
                        Delete
                      </Button>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <ProviderFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        provider={editing}
        onSaved={load}
      />

      {/* Soft-delete confirmation */}
      <Dialog open={pending !== null} onOpenChange={(o) => (o ? undefined : closeDelete())}>
        <DialogContent showCloseButton={!busy} className="max-w-sm gap-0 p-0">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">Deactivate provider?</DialogTitle>
            <DialogDescription>
              {pending && (
                <>
                  <span className="font-medium text-foreground">{pending.name}</span> will be marked
                  inactive and excluded from call routing. Its IVR playbooks are kept, and you can
                  re-activate it later by editing its status.
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          {dialogError && (
            <p className="px-5 pb-1 text-sm text-destructive" role="alert">{dialogError}</p>
          )}
          <div className="flex justify-end gap-3 border-t border-border p-4">
            <Button variant="outline" onClick={closeDelete} disabled={busy}>Cancel</Button>
            <Button
              variant="destructive"
              onClick={confirmDelete}
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
