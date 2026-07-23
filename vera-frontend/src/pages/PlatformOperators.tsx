import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { InvitePlatformOperatorDialog } from "@/components/platform/InvitePlatformOperatorDialog"
import { ApiError } from "@/lib/api/client"
import {
  deactivateOperator, listOperators, resendOperatorInvitation, type Operator,
} from "@/lib/api/platform"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline"> = {
  active: "default",
  invited: "secondary",
  deactivated: "outline",
}

export function PlatformOperators() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [operators, setOperators] = useState<Operator[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [resendingId, setResendingId] = useState<string | null>(null)

  const [pending, setPending] = useState<Operator | null>(null)
  const [busy, setBusy] = useState(false)
  const [dialogError, setDialogError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      setOperators(await listOperators())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load platform operators.")
    }
  }, [])

  // Initial load. setState only in the async callbacks (not synchronously in the
  // effect body), with a cancelled flag to avoid a post-unmount update.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listOperators()
      .then((ops) => {
        if (!cancelled) setOperators(ops)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load platform operators.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  // Auto-dismiss the success notice after a few seconds.
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

  const activeCount = operators?.filter((o) => o.status === "active").length ?? 0

  async function onResend(operator: Operator) {
    setResendingId(operator.id)
    setError(null)
    try {
      await resendOperatorInvitation(operator.id)
      setNotice(`A fresh invite link was sent to ${operator.name || operator.email}.`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not resend the invitation.")
    } finally {
      setResendingId(null)
    }
  }

  function askDeactivate(operator: Operator) {
    setNotice(null)
    setDialogError(null)
    setPending(operator)
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
      await deactivateOperator(pending.id)
      setPending(null)
      await load()
      setNotice(`${label} has been deactivated.`)
    } catch (err) {
      setDialogError(err instanceof ApiError ? err.message : "Could not deactivate operator.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Platform Operators</h1>
          <p className="text-sm text-muted-foreground">
            {operators ? `${activeCount} active` : "Loading…"} — every operator holds full
            super-admin access.
          </p>
        </div>
        <InvitePlatformOperatorDialog onInvited={load} />
      </div>

      {notice && <p className="text-sm text-emerald-600 dark:text-emerald-400" role="status">{notice}</p>}
      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {operators === null && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {operators?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  No platform operators yet.
                </TableCell>
              </TableRow>
            )}
            {operators?.map((o) => {
              const isLastActive = o.status === "active" && activeCount <= 1
              return (
                <TableRow key={o.id}>
                  <TableCell className="font-medium">{o.name || "—"}</TableCell>
                  <TableCell>{o.email}</TableCell>
                  <TableCell>
                    <Badge variant={STATUS_VARIANT[o.status] ?? "outline"} className="capitalize">
                      {o.status}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {o.status === "invited" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="-ml-2 mr-1"
                        onClick={() => onResend(o)}
                        disabled={resendingId === o.id}
                      >
                        {resendingId === o.id ? "Resending…" : "Resend invitation"}
                      </Button>
                    )}
                    {o.status !== "deactivated" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:text-destructive"
                        onClick={() => askDeactivate(o)}
                        disabled={isLastActive}
                        title={isLastActive ? "Cannot deactivate the last active operator" : undefined}
                      >
                        Deactivate
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <Dialog open={pending !== null} onOpenChange={(o) => (o ? undefined : closeDialog())}>
        <DialogContent showCloseButton={!busy} className="max-w-sm gap-0 p-0">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">Deactivate operator?</DialogTitle>
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
