import { useCallback, useEffect, useState, type FormEvent } from "react"
import { KeyRound, Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/client"
import {
  createElevation,
  endElevation,
  listElevations,
  MAX_ELEVATION_MINUTES,
  MAX_ELEVATION_REASON,
  type Elevation,
} from "@/lib/api/platform"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const DEFAULT_DURATION = 60

/** Human "expires in …" for a grant, recomputed on each render. */
function expiresLabel(expiresAt: string): string {
  const ms = new Date(expiresAt).getTime() - Date.now()
  if (ms <= 0) return "expired"
  const min = Math.round(ms / 60000)
  if (min < 60) return `expires in ${min}m`
  return `expires in ${Math.floor(min / 60)}h ${min % 60}m`
}

export function TenantAccess() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)

  const [elevations, setElevations] = useState<Elevation[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  const [tenantId, setTenantId] = useState("")
  const [reason, setReason] = useState("")
  const [duration, setDuration] = useState(DEFAULT_DURATION)
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [endingId, setEndingId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      setElevations(await listElevations())
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Could not load elevations.")
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial load. setState only in the async callbacks (not synchronously in the
  // effect body), with a cancelled flag to avoid a post-unmount update.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listElevations()
      .then((rows) => {
        if (!cancelled) setElevations(rows)
      })
      .catch((err) => {
        if (!cancelled)
          setLoadError(err instanceof ApiError ? err.message : "Could not load elevations.")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  // Platform-only surface; the backend also enforces this, but hide it cleanly.
  if (!isSuperAdmin) {
    return <p className="text-sm text-muted-foreground">This page is only available to platform operators.</p>
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setFormError(null)
    const id = tenantId.trim()
    if (!UUID_RE.test(id)) {
      setFormError("Enter a valid tenant ID (UUID).")
      return
    }
    if (reason.trim().length === 0) {
      setFormError("A reason is required.")
      return
    }
    if (duration < 1 || duration > MAX_ELEVATION_MINUTES) {
      setFormError(`Duration must be between 1 and ${MAX_ELEVATION_MINUTES} minutes.`)
      return
    }
    setBusy(true)
    try {
      await createElevation({
        target_tenant_id: id,
        reason: reason.trim(),
        duration_minutes: duration,
      })
      setTenantId("")
      setReason("")
      setDuration(DEFAULT_DURATION)
      await refresh()
    } catch (err) {
      // 409 = operator already holds an active grant.
      setFormError(
        err instanceof ApiError && err.httpStatus === 409
          ? "You already hold an active elevation. End it before requesting another."
          : err instanceof ApiError
            ? err.message
            : "Could not create the elevation.",
      )
    } finally {
      setBusy(false)
    }
  }

  async function onEnd(id: string) {
    setEndingId(id)
    setLoadError(null)
    try {
      await endElevation(id)
      await refresh()
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Could not end the elevation.")
    } finally {
      setEndingId(null)
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Tenant Access</h1>
        <p className="text-sm text-muted-foreground">
          Elevate into a tenant for a time-boxed window to operate on its data. Access ends
          automatically when the grant expires, or when you end it.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <KeyRound className="size-4 text-muted-foreground" />
            Active elevations
          </CardTitle>
          <CardDescription>Grants currently open across platform operators.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {loadError && (
            <Alert variant="destructive">
              <AlertDescription>{loadError}</AlertDescription>
            </Alert>
          )}
          {loading ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Loading…
            </p>
          ) : elevations.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No active elevations. Request one below to operate inside a tenant.
            </p>
          ) : (
            <ul className="space-y-3">
              {elevations.map((e) => (
                <li
                  key={e.id}
                  className="flex items-start justify-between gap-4 rounded-md border p-3"
                >
                  <div className="min-w-0 space-y-1">
                    <p className="truncate font-mono text-sm">{e.target_tenant_id}</p>
                    <p className="truncate text-sm text-muted-foreground">{e.reason}</p>
                    <Badge variant="secondary">{expiresLabel(e.expires_at)}</Badge>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => onEnd(e.id)}
                    disabled={endingId === e.id}
                  >
                    {endingId === e.id ? <Loader2 className="animate-spin" /> : null}
                    End
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Request access</CardTitle>
          <CardDescription>
            One active elevation per operator. Max {MAX_ELEVATION_MINUTES / 60} hours.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onCreate} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="tenant-id">Tenant ID</Label>
              <Input
                id="tenant-id"
                placeholder="00000000-0000-0000-0000-000000000000"
                value={tenantId}
                onChange={(ev) => setTenantId(ev.target.value)}
                className="font-mono"
              />
              <p className="text-xs text-muted-foreground">
                The tenant's UUID. (A tenant picker arrives once the backend exposes a list
                endpoint.)
              </p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="reason">Reason</Label>
              <Textarea
                id="reason"
                placeholder="Why you need access (recorded in the audit log)."
                maxLength={MAX_ELEVATION_REASON}
                value={reason}
                onChange={(ev) => setReason(ev.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="duration">Duration (minutes)</Label>
              <Input
                id="duration"
                type="number"
                min={1}
                max={MAX_ELEVATION_MINUTES}
                value={duration}
                onChange={(ev) => setDuration(Number(ev.target.value))}
                className="max-w-32"
              />
            </div>
            {formError && (
              <Alert variant="destructive">
                <AlertDescription>{formError}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" disabled={busy}>
              {busy ? <Loader2 className="animate-spin" /> : <KeyRound />}
              {busy ? "Requesting…" : "Request access"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
