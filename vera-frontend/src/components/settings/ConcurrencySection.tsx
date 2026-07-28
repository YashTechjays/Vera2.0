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

const KNOBS = [
  {
    key: "max_agents_per_va",
    id: "max-agents-per-va",
    label: "Agents per VA (1–20)",
    min: 1,
    max: 20,
  },
  {
    key: "max_concurrent_calls",
    id: "max-concurrent-calls",
    label: "Tenant call ceiling (1–100)",
    min: 1,
    max: 100,
  },
] as const satisfies ReadonlyArray<{
  key: keyof ConcurrencyConfig
  id: string
  label: string
  min: number
  max: number
}>

/** Admin knobs for agent concurrency: per-VA in-flight cap + tenant dial ceiling. */
export function ConcurrencySection() {
  const [config, setConfig] = useState<ConcurrencyConfig | null>(null)
  const [saved, setSaved] = useState<ConcurrencyConfig | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getConcurrencyConfig()
      .then((loaded) => {
        setConfig(loaded)
        setSaved(loaded)
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load capacity settings."),
      )
  }, [])

  const save = async () => {
    if (!config || !saved) return
    // PATCH only the edited knobs — resending unchanged fields from a stale copy
    // would silently revert another admin's concurrent change (last-write-wins).
    const patch: Partial<ConcurrencyConfig> = {}
    for (const { key } of KNOBS) {
      if (config[key] !== saved[key]) patch[key] = config[key]
    }
    if (Object.keys(patch).length === 0) return
    setSaving(true)
    setError(null)
    try {
      const next = await patchConcurrencyConfig(patch)
      setConfig(next)
      setSaved(next)
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
          {KNOBS.map((knob) => (
            <div className="grid gap-1.5" key={knob.key}>
              <Label htmlFor={knob.id}>{knob.label}</Label>
              <Input
                id={knob.id}
                type="number"
                min={knob.min}
                max={knob.max}
                value={config[knob.key]}
                onChange={(e) => setKnob(knob.key, e.target.value)}
                className="max-w-32"
              />
            </div>
          ))}
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
