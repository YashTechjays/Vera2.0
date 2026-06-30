import { useCallback, useEffect, useState, type FormEvent } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { configureIntegration, listIntegrations, type Integration } from "@/lib/integrations"
import { formatDate } from "@/lib/patient-forms/display"

// The single integration this tenant configures. The integration-type catalog is
// not exposed over the API, so the frontend names the slug + its one credential
// key directly. Both are seeded server-side (scripts/seed.py:
// livekit_outbound_trunk_id → {trunk_id}).
const INTEGRATION_TYPE = "livekit_outbound_trunk_id"
const CREDENTIAL_KEY = "trunk_id"
const DISPLAY_NAME = "LiveKit outbound trunk"

/** Manage the tenant's single outbound integration credential: view configured
 *  state, set/replace the write-once secret. The value is never returned by the
 *  API. Mount only behind an `integrations:manage` check — gated server-side too. */
export function IntegrationsSection() {
  const [integration, setIntegration] = useState<Integration | null | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)

  const [value, setValue] = useState("")
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let cancelled = false
    listIntegrations()
      .then((rows) => {
        if (!cancelled) {
          setIntegration(rows.find((r) => r.integration_type === INTEGRATION_TYPE) ?? null)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load integrations.")
          setIntegration(null)
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleSave = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      if (!value.trim()) return
      setSaving(true)
      setSaveError(null)
      setSaved(false)
      try {
        const updated = await configureIntegration(INTEGRATION_TYPE, {
          [CREDENTIAL_KEY]: value.trim(),
        })
        setIntegration(updated)
        setValue("")
        setSaved(true)
      } catch (err) {
        setSaveError(err instanceof ApiError ? err.message : "Could not save the credential.")
      } finally {
        setSaving(false)
      }
    },
    [value],
  )

  const configured = integration?.configured ?? false

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-sm font-medium">Integrations</h2>
        <p className="text-sm text-muted-foreground">
          Outbound credential Vera uses to reach a third party. The value is stored encrypted and
          never shown again after you save it.
        </p>
      </div>

      <div className="space-y-3 rounded-lg border p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium">{DISPLAY_NAME}</span>
          <span
            className={cn(
              "inline-block rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
              configured ? "bg-emerald-100 text-emerald-700" : "bg-muted text-muted-foreground",
            )}
          >
            {integration === undefined ? "Loading…" : configured ? "Configured" : "Not configured"}
          </span>
        </div>

        {configured && integration?.rotated_at && (
          <p className="text-xs text-muted-foreground">
            Last updated {formatDate(integration.rotated_at)}
          </p>
        )}

        {/* Write-once secret — the field is always empty on load (the value is never returned). */}
        <form className="flex flex-wrap items-end gap-2" onSubmit={handleSave}>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">
              {configured ? "Replace trunk id" : "Set trunk id"}
            </label>
            <Input
              type="password"
              autoComplete="off"
              value={value}
              onChange={(e) => {
                setValue(e.target.value)
                setSaved(false)
              }}
              placeholder="Paste the credential value"
              className="w-80"
            />
          </div>
          <Button type="submit" disabled={saving || !value.trim()}>
            {saving ? "Saving…" : "Save"}
          </Button>
          {saved && <span className="text-sm text-emerald-700">Saved.</span>}
        </form>

        {saveError && (
          <p className="text-sm text-destructive" role="alert">
            {saveError}
          </p>
        )}
        {error && (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>
    </section>
  )
}
