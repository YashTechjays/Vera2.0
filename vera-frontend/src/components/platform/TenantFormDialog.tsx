import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage } from "@/lib/api/client"
import { createTenant, updateTenant, type TenantDetail } from "@/lib/api/platform"
import {
  changedTenantFields,
  isValidSlug,
  SLUG_MAX_LENGTH,
  slugify,
  type TenantFormValues,
} from "@/pages/platformTenants.helpers"

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** null = create a new tenant; a detail = edit that tenant. */
  tenant: TenantDetail | null
  onSaved?: () => void
}

// Only name/region reach the API in create mode; the config values below exist to
// satisfy TenantFormValues — the server owns a new tenant's config defaults.
const CREATE_DEFAULTS: TenantFormValues = {
  name: "",
  region: null,
  observer_enabled: true,
  auto_retry_enabled: true,
  retry_fill_threshold: 0.5,
  max_agents_per_va: 3,
  max_concurrent_calls: 25,
  max_retries: 5,
  queue_expiry_hours: 48,
  recording_retention_days: null,
}

const NUMBER_FIELDS = [
  { key: "max_agents_per_va", label: "Max agents per VA", min: 1, step: 1 },
  { key: "max_concurrent_calls", label: "Max concurrent calls", min: 1, step: 1 },
  { key: "max_retries", label: "Max retries", min: 0, step: 1 },
  { key: "queue_expiry_hours", label: "Queue expiry (hours)", min: 1, step: 1 },
  { key: "retry_fill_threshold", label: "Retry fill threshold (0–1)", min: 0, step: 0.05 },
] as const

const CHECKBOX_FIELDS = [
  { key: "observer_enabled", label: "AI form-filling (observer) enabled" },
  { key: "auto_retry_enabled", label: "Auto-retry low-fill calls" },
] as const

export function TenantFormDialog({ open, onOpenChange, tenant, onSaved }: Props) {
  const [busy, setBusy] = useState(false)
  return (
    <Dialog open={open} onOpenChange={(o) => (busy ? undefined : onOpenChange(o))}>
      {/* Mounted only while open, so the form's useState initializers reload it each
          time and a previous tenant's values can never leak in. */}
      {open && (
        <TenantForm
          tenant={tenant}
          busy={busy}
          setBusy={setBusy}
          close={() => onOpenChange(false)}
          onSaved={onSaved}
        />
      )}
    </Dialog>
  )
}

type TenantFormProps = {
  tenant: TenantDetail | null
  busy: boolean
  setBusy: (busy: boolean) => void
  close: () => void
  onSaved?: () => void
}

function TenantForm({ tenant, busy, setBusy, close, onSaved }: TenantFormProps) {
  const isEdit = tenant !== null
  const [fields, setFields] = useState<TenantFormValues>(tenant ? { ...tenant } : CREATE_DEFAULTS)
  const [slug, setSlug] = useState(tenant?.slug ?? "")
  const [slugEdited, setSlugEdited] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function set<K extends keyof TenantFormValues>(key: K, value: TenantFormValues[K]) {
    setFields((prev) => ({ ...prev, [key]: value }))
  }

  function onNameChange(value: string) {
    set("name", value)
    // Keep the slug in step with the name until the operator types their own.
    if (!isEdit && !slugEdited) setSlug(slugify(value))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (!fields.name.trim()) {
      setError("Name is required.")
      return
    }
    if (!isEdit && !isValidSlug(slug)) {
      setError("Slug must be lowercase letters, digits and hyphens (max 63 characters).")
      return
    }
    setBusy(true)
    try {
      if (tenant) {
        const patch = changedTenantFields(tenant, fields)
        if (Object.keys(patch).length === 0) {
          close()
          return
        }
        await updateTenant(tenant.id, patch)
      } else {
        await createTenant({
          name: fields.name.trim(),
          slug,
          ...(fields.region ? { region: fields.region } : {}),
        })
      }
      onSaved?.()
      close()
    } catch (err) {
      setError(apiErrorMessage(err, "Could not save the tenant."))
    } finally {
      setBusy(false)
    }
  }

  const submitLabel = isEdit ? "Save changes" : "Create tenant"

  return (
    <DialogContent showCloseButton={!busy} className="max-w-lg gap-0 p-0">
      <DialogHeader className="border-b border-border p-5 pr-12">
        <DialogTitle className="text-base font-semibold">
          {isEdit ? "Edit tenant" : "New tenant"}
        </DialogTitle>
        <DialogDescription>
          {isEdit
            ? "The slug cannot change — it is part of this tenant's login URL."
            : "Creates the organisation only. Invite its first user afterwards."}
        </DialogDescription>
      </DialogHeader>

      <form onSubmit={onSubmit}>
        <div className="max-h-[60vh] space-y-4 overflow-y-auto p-5">
          <div className="space-y-1.5">
            <Label htmlFor="tenant-name">Name</Label>
            <Input
              id="tenant-name"
              required
              autoFocus
              placeholder="Acme Health"
              value={fields.name}
              onChange={(e) => onNameChange(e.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="tenant-slug">Slug</Label>
            <Input
              id="tenant-slug"
              required
              disabled={isEdit}
              maxLength={SLUG_MAX_LENGTH}
              placeholder="acme-health"
              value={slug}
              onChange={(e) => {
                setSlugEdited(true)
                setSlug(e.target.value.toLowerCase())
              }}
              className="font-mono text-sm"
            />
            <p className="text-xs text-muted-foreground">
              {isEdit ? "Immutable after creation." : "Used in this tenant's login URL."}
            </p>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="tenant-region">Region</Label>
            <Input
              id="tenant-region"
              placeholder="us-east"
              value={fields.region ?? ""}
              onChange={(e) => set("region", e.target.value.trim() === "" ? null : e.target.value)}
            />
          </div>

          {isEdit && (
            <>
              <div className="grid grid-cols-2 gap-4">
                {NUMBER_FIELDS.map(({ key, label, min, step }) => (
                  <div key={key} className="space-y-1.5">
                    <Label htmlFor={`tenant-${key}`}>{label}</Label>
                    <Input
                      id={`tenant-${key}`}
                      type="number"
                      min={min}
                      step={step}
                      value={fields[key]}
                      onChange={(e) => set(key, Number(e.target.value))}
                    />
                  </div>
                ))}
                <div className="space-y-1.5">
                  <Label htmlFor="tenant-recording_retention_days">
                    Recording retention (days)
                  </Label>
                  <Input
                    id="tenant-recording_retention_days"
                    type="number"
                    min={1}
                    placeholder="unset"
                    value={fields.recording_retention_days ?? ""}
                    onChange={(e) =>
                      set(
                        "recording_retention_days",
                        e.target.value === "" ? null : Number(e.target.value),
                      )
                    }
                  />
                </div>
              </div>

              {CHECKBOX_FIELDS.map(({ key, label }) => (
                <div key={key} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    id={`tenant-${key}`}
                    checked={fields[key]}
                    onCheckedChange={(c) => set(key, c === true)}
                  />
                  <Label htmlFor={`tenant-${key}`} className="font-normal">
                    {label}
                  </Label>
                </div>
              ))}
            </>
          )}

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-3 border-t border-border p-4">
          <Button type="button" variant="outline" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} className="min-w-[120px]">
            {busy ? "Saving…" : submitLabel}
          </Button>
        </div>
      </form>
    </DialogContent>
  )
}
