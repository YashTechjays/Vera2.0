import { useEffect, useState } from "react"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { Switch } from "@/components/ui/switch"
import { ApiError } from "@/lib/api/client"
import { listTenants, setTenantObserverEnabled, type TenantSummary } from "@/lib/api/platform"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

export function PlatformSettings() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [tenants, setTenants] = useState<TenantSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [savingId, setSavingId] = useState<string | null>(null)

  // Initial load. setState only in the async callbacks, with a cancelled flag to
  // avoid a post-unmount update (mirrors PlatformOperators).
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listTenants()
      .then((ts) => {
        if (!cancelled) setTenants(ts)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load tenants.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  // Platform-only surface; the backend also enforces this, but hide it cleanly.
  if (!isSuperAdmin) {
    return (
      <p className="text-sm text-muted-foreground">
        This page is only available to platform operators.
      </p>
    )
  }

  async function onToggle(tenant: TenantSummary, next: boolean) {
    setError(null)
    setSavingId(tenant.id)
    // Optimistic: flip in place, revert on failure.
    setTenants((prev) =>
      prev?.map((t) => (t.id === tenant.id ? { ...t, observer_enabled: next } : t)) ?? prev,
    )
    try {
      await setTenantObserverEnabled(tenant.id, next)
    } catch (err) {
      setTenants((prev) =>
        prev?.map((t) => (t.id === tenant.id ? { ...t, observer_enabled: !next } : t)) ?? prev,
      )
      setError(err instanceof ApiError ? err.message : "Could not update the tenant.")
    } finally {
      setSavingId(null)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Platform Settings</h1>
        <p className="text-sm text-muted-foreground">
          Toggle AI form filling per tenant. When off, the agent still runs the call but
          stops extracting answers to auto-fill forms — completion is left to manual review.
        </p>
      </div>

      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Tenant</TableHead>
              <TableHead>Slug</TableHead>
              <TableHead className="text-right">AI form filling</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tenants === null && (
              <TableRow>
                <TableCell colSpan={3} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {tenants?.length === 0 && (
              <TableRow>
                <TableCell colSpan={3} className="py-6 text-center text-muted-foreground">
                  No active tenants.
                </TableCell>
              </TableRow>
            )}
            {tenants?.map((t) => (
              <TableRow key={t.id}>
                <TableCell className="font-medium">{t.name}</TableCell>
                <TableCell className="font-mono text-sm text-muted-foreground">{t.slug}</TableCell>
                <TableCell className="text-right">
                  <Switch
                    checked={t.observer_enabled}
                    disabled={savingId === t.id}
                    onCheckedChange={(v) => void onToggle(t, v)}
                    aria-label={`AI form filling for ${t.name}`}
                  />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
