import { useEffect, useState } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import {
  getConcurrencyConfig,
  patchConcurrencyConfig,
  type ConcurrencyConfig,
} from "@/lib/api/tenantConfig"
import { SettingsCard } from "./SettingsCard"

/** Admin knobs for agent concurrency: per-VA in-flight cap + tenant dial ceiling. */
export function ConcurrencySection() {
  const [config, setConfig] = useState<ConcurrencyConfig | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getConcurrencyConfig()
      .then(setConfig)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load capacity settings."),
      )
  }, [])

  const save = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    try {
      setConfig(await patchConcurrencyConfig(config))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save capacity settings.")
    } finally {
      setSaving(false)
    }
  }

  const setKnob = (key: keyof ConcurrencyConfig, raw: string) => {
    const value = Number(raw)
    if (config && Number.isInteger(value)) setConfig({ ...config, [key]: value })
  }

  const ceilingBelowPerVa =
    config !== null && config.max_concurrent_calls < config.max_agents_per_va

  return (
    <SettingsCard
      title="Agent capacity"
      description="How many agent calls each VA may run at once, and the tenant-wide ceiling across all VAs."
    >
      {config && (
        <div className="space-y-4">
          <div className="grid gap-1.5">
            <Label htmlFor="max-agents-per-va">Agents per VA (1–20)</Label>
            <Input
              id="max-agents-per-va"
              type="number"
              min={1}
              max={20}
              value={config.max_agents_per_va}
              onChange={(e) => setKnob("max_agents_per_va", e.target.value)}
              className="max-w-32"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="max-concurrent-calls">Tenant call ceiling (1–100)</Label>
            <Input
              id="max-concurrent-calls"
              type="number"
              min={1}
              max={100}
              value={config.max_concurrent_calls}
              onChange={(e) => setKnob("max_concurrent_calls", e.target.value)}
              className="max-w-32"
            />
          </div>
          {ceilingBelowPerVa && (
            <p className="text-sm text-muted-foreground">
              The tenant ceiling is below the per-VA limit, so the ceiling will apply first.
            </p>
          )}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          <Button onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </div>
      )}
      {!config && error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </SettingsCard>
  )
}
