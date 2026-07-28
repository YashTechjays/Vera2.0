import { useEffect, useState } from "react"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { Switch } from "@/components/ui/switch"
import { ApiError } from "@/lib/api/client"
import { listTenants, setTenantObserverEnabled, type TenantSummary } from "@/lib/api/platform"
import { usePermission } from "@/lib/auth/permissions"

export function PlatformSettings() {
  // Gate on the permission that actually governs this screen, matching the nav entry in
  // lib/nav.ts — account_type === "platform" would render the table for any platform
  // operator, whose toggles would then 403 from the backend.
  const mayManage = usePermission("platform:tenants:manage")
  const [tenants, setTenants] = useState<TenantSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [savingId, setSavingId] = useState<string | null>(null)

  // Initial load. setState only in the async callbacks, with a cancelled flag to
  // avoid a post-unmount update (mirrors PlatformOperators).
  useEffect(() => {
    if (!mayManage) return
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
  }, [mayManage])

  // The backend also enforces this, but hide the surface cleanly.
  if (!mayManage) {
    return (
      <p className="text-sm text-muted-foreground">
        You do not have permission to manage tenant settings.
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
          Conditional questions then follow the intake prefill rather than the
          representative&rsquo;s live answers.
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
                  {/* `?? false` only satisfies the tri-state wire type: the page is gated
                      on the same permission that controls disclosure, so null never
                      reaches here. */}
                  <Switch
                    checked={t.observer_enabled ?? false}
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
