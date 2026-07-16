import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { ApiError } from "@/lib/api/client"
import {
  createProvider, updateProvider, type ProviderStatus, type ProviderSummary,
} from "@/lib/api/insuranceProviders"

/** A native <input type="time"> speaks "HH:MM"; the API stores/returns "HH:MM:SS".
 *  Trim to the minute for the input, and treat an empty field as null (cleared). */
function toInputTime(value: string | null): string {
  return value ? value.slice(0, 5) : ""
}

/** Create/edit form for an insurance provider. Controlled by the parent so it can be
 *  opened from a "New provider" button (provider = null) or a row's Edit action. The
 *  inner form is mounted only while open, so it initializes fresh from `provider` each
 *  time without syncing state in an effect. */
export function ProviderFormDialog({
  open,
  onOpenChange,
  provider,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  provider: ProviderSummary | null
  onSaved: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent showCloseButton className="max-w-md gap-0 p-0">
        {open && (
          <ProviderForm
            provider={provider}
            onSaved={onSaved}
            onClose={() => onOpenChange(false)}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function ProviderForm({
  provider,
  onSaved,
  onClose,
}: {
  provider: ProviderSummary | null
  onSaved: () => void
  onClose: () => void
}) {
  const [name, setName] = useState(provider?.name ?? "")
  const [start, setStart] = useState(toInputTime(provider?.working_hour_start ?? null))
  const [end, setEnd] = useState(toInputTime(provider?.working_hour_end ?? null))
  const [status, setStatus] = useState<ProviderStatus>(provider?.status ?? "active")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const isEdit = provider !== null

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (name.trim() === "") {
      setError("Name is required.")
      return
    }
    setError(null)
    setBusy(true)
    // Empty time fields are sent as null so a cleared field clears the stored value.
    const payload = {
      name: name.trim(),
      working_hour_start: start || null,
      working_hour_end: end || null,
      status,
    }
    try {
      if (isEdit) {
        await updateProvider(provider.id, payload)
      } else {
        await createProvider(payload)
      }
      onSaved()
      onClose()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save provider.")
      setBusy(false)
    }
  }

  return (
    <>
      <DialogHeader className="border-b border-border p-5 pr-12">
        <DialogTitle className="text-base font-semibold">
          {isEdit ? "Edit provider" : "New provider"}
        </DialogTitle>
        <DialogDescription>
          Insurance providers are shared across tenants and steer IVR playbooks.
        </DialogDescription>
      </DialogHeader>
      <form onSubmit={onSubmit}>
        <div className="space-y-4 p-5">
          <div className="space-y-1.5">
            <Label htmlFor="provider-name">Name</Label>
            <Input
              id="provider-name"
              required
              autoFocus
              placeholder="e.g. UnitedHealthcare"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="provider-start">Working hours start</Label>
              <Input
                id="provider-start"
                type="time"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="provider-end">Working hours end</Label>
              <Input
                id="provider-end"
                type="time"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="provider-status">Status</Label>
            <Select
              id="provider-status"
              value={status}
              onChange={(e) => setStatus(e.target.value as ProviderStatus)}
            >
              <option value="active">Active</option>
              <option value="inactive">Inactive</option>
            </Select>
          </div>
          {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        </div>
        <div className="flex justify-end gap-3 border-t border-border p-4">
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy} className="min-w-[120px]">
            {busy ? "Saving…" : isEdit ? "Save changes" : "Create provider"}
          </Button>
        </div>
      </form>
    </>
  )
}
