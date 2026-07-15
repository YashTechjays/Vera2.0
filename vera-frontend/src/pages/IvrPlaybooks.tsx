import { useCallback, useEffect, useState, type FormEvent } from "react"
import { Loader2, Trash2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/client"
import { listProviders, type ProviderSummary } from "@/lib/api/insuranceProviders"
import {
  createPlaybook,
  deletePlaybook,
  getPlaybook,
  listPlaybooks,
  updatePlaybook,
  type IvrPlaybookInstructions,
  type PlaybookSummary,
} from "@/lib/api/ivrPlaybooks"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

/** The playbook config fields, in editor display order. Each is a free-text override that
 *  specializes the generic IVR navigator; an empty field falls back to its built-in defaults. */
const INSTRUCTION_FIELDS: {
  key: keyof IvrPlaybookInstructions
  label: string
  placeholder: string
}[] = [
  {
    key: "provider_subflows",
    label: "Provider subflows",
    placeholder: "Provider-specific ID/menu sub-flows (e.g. Cigna ID-letter flow)",
  },
  {
    key: "extra_rules",
    label: "Provider-specific rules",
    placeholder:
      "Free-text overrides for this provider — e.g. reach-a-human keyword (UHC: “Advocate”), survey answer, date scope",
  },
]

/** Keep only non-empty (trimmed) fields so unset knobs stay unset server-side. */
function cleanInstructions(instructions: IvrPlaybookInstructions): IvrPlaybookInstructions {
  const cleaned: IvrPlaybookInstructions = {}
  for (const { key } of INSTRUCTION_FIELDS) {
    const value = instructions[key]?.trim()
    if (value) cleaned[key] = value
  }
  return cleaned
}

/** Inline destructive banner for a request error; renders nothing when there's none. */
function ErrorAlert({ error }: { error: string | null }) {
  if (!error) return null
  return (
    <Alert variant="destructive">
      <AlertDescription>{error}</AlertDescription>
    </Alert>
  )
}

/** Spinner + "Loading…" line, shared by the provider and playbook sections. */
function LoadingLine() {
  return (
    <p className="flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> Loading…
    </p>
  )
}

export function IvrPlaybooks() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)

  const [providers, setProviders] = useState<ProviderSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Selected provider + its playbooks.
  const [selectedProviderId, setSelectedProviderId] = useState("")
  const [playbooks, setPlaybooks] = useState<PlaybookSummary[]>([])
  const [playbooksLoading, setPlaybooksLoading] = useState(false)
  const [playbooksError, setPlaybooksError] = useState<string | null>(null)

  // Editor (create when editingId is null, else edit).
  const [editingId, setEditingId] = useState<string | null>(null)
  const [instructions, setInstructions] = useState<IvrPlaybookInstructions>({})
  const [status, setStatus] = useState("active")
  const [editorBusy, setEditorBusy] = useState(false)
  const [editorError, setEditorError] = useState<string | null>(null)

  // Delete confirmation.
  const [pendingDelete, setPendingDelete] = useState<PlaybookSummary | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listProviders()
      .then((rows) => {
        if (!cancelled) setProviders(rows)
      })
      .catch((err) => {
        if (!cancelled)
          setLoadError(err instanceof ApiError ? err.message : "Could not load providers.")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  const resetEditor = useCallback(() => {
    setEditingId(null)
    setInstructions({})
    setStatus("active")
    setEditorError(null)
  }, [])

  const loadPlaybooks = useCallback(async (providerId: string) => {
    setPlaybooksLoading(true)
    setPlaybooksError(null)
    try {
      setPlaybooks(await listPlaybooks(providerId))
    } catch (err) {
      setPlaybooksError(err instanceof ApiError ? err.message : "Could not load playbooks.")
    } finally {
      setPlaybooksLoading(false)
    }
  }, [])

  // Re-fetch playbooks when the selected provider changes. State is set only in the async
  // callbacks (never synchronously in the effect body); the loading flag is flipped on by the
  // handler that changes the provider (selectProvider), mirroring AgentPrompt's effect.
  useEffect(() => {
    if (!selectedProviderId) return
    let cancelled = false
    listPlaybooks(selectedProviderId)
      .then((rows) => {
        if (!cancelled) setPlaybooks(rows)
      })
      .catch((err) => {
        if (!cancelled)
          setPlaybooksError(err instanceof ApiError ? err.message : "Could not load playbooks.")
      })
      .finally(() => {
        if (!cancelled) setPlaybooksLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedProviderId])

  // Switch the active provider: flip loading on here (an event handler, so setState is fine)
  // and let the effect above resolve it.
  const selectProvider = useCallback(
    (providerId: string) => {
      setSelectedProviderId(providerId)
      setPlaybooksError(null)
      setPlaybooksLoading(providerId !== "")
      resetEditor()
    },
    [resetEditor],
  )

  // Platform-only surface; the backend enforces this too, but hide it cleanly.
  if (!isSuperAdmin) {
    return (
      <p className="text-sm text-muted-foreground">
        This page is only available to platform operators.
      </p>
    )
  }

  function setField(key: keyof IvrPlaybookInstructions, value: string) {
    setInstructions((prev) => ({ ...prev, [key]: value }))
  }

  async function onEdit(id: string) {
    setEditorError(null)
    try {
      const detail = await getPlaybook(id)
      setEditingId(detail.id)
      setInstructions(detail.instructions ?? {})
      setStatus(detail.status)
    } catch (err) {
      setEditorError(err instanceof ApiError ? err.message : "Could not load the playbook.")
    }
  }

  async function onSave(e: FormEvent) {
    e.preventDefault()
    setEditorError(null)
    if (!selectedProviderId) {
      setEditorError("Select a provider first.")
      return
    }
    setEditorBusy(true)
    try {
      const cleaned = cleanInstructions(instructions)
      if (editingId) {
        await updatePlaybook(editingId, { instructions: cleaned, status })
      } else {
        await createPlaybook({ provider_id: selectedProviderId, instructions: cleaned, status })
      }
      resetEditor()
      await loadPlaybooks(selectedProviderId)
    } catch (err) {
      // 409 = another active playbook won the one-active-per-provider race.
      setEditorError(err instanceof ApiError ? err.message : "Could not save the playbook.")
    } finally {
      setEditorBusy(false)
    }
  }

  async function confirmDelete() {
    if (pendingDelete === null) return
    setDeleteBusy(true)
    setDeleteError(null)
    try {
      await deletePlaybook(pendingDelete.id)
      if (editingId === pendingDelete.id) resetEditor()
      setPendingDelete(null)
      await loadPlaybooks(selectedProviderId)
    } catch (err) {
      setDeleteError(err instanceof ApiError ? err.message : "Could not delete the playbook.")
    } finally {
      setDeleteBusy(false)
    }
  }

  const selectedProvider = providers.find((p) => p.id === selectedProviderId)

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">IVR Playbooks</h1>
        <p className="text-sm text-muted-foreground">
          Per-provider navigation overlays that specialize the generic IVR navigator. At most one
          active playbook per provider; activating another demotes the previous one.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Playbooks</CardTitle>
          <CardDescription>Select a provider to manage its playbooks.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <ErrorAlert error={loadError} />
          <div className="space-y-1.5">
            <Label htmlFor="provider-select">Provider</Label>
            {loading ? (
              <LoadingLine />
            ) : (
              <Select
                id="provider-select"
                value={selectedProviderId}
                onChange={(ev) => selectProvider(ev.target.value)}
              >
                <option value="">Select a provider…</option>
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </Select>
            )}
          </div>

          {selectedProviderId && (
            <>
              <ErrorAlert error={playbooksError} />
              {playbooksLoading ? (
                <LoadingLine />
              ) : playbooks.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No playbooks yet for {selectedProvider?.name}. Create one below.
                </p>
              ) : (
                <ul className="space-y-2">
                  {playbooks.map((pb) => (
                    <li
                      key={pb.id}
                      className="flex items-center justify-between gap-3 rounded-md border p-3"
                    >
                      <div className="flex items-center gap-2">
                        <Badge variant={pb.status === "active" ? "default" : "secondary"}>
                          {pb.status}
                        </Badge>
                        <span className="font-mono text-xs text-muted-foreground">{pb.id}</span>
                      </div>
                      <div className="flex gap-2">
                        <Button variant="outline" size="sm" onClick={() => onEdit(pb.id)}>
                          Edit
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setPendingDelete(pb)
                            setDeleteError(null)
                          }}
                        >
                          <Trash2 />
                        </Button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}

              <form onSubmit={onSave} className="space-y-4 border-t pt-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium">
                    {editingId ? "Edit playbook" : "New playbook"}
                  </h3>
                  {editingId && (
                    <Button type="button" variant="ghost" size="sm" onClick={resetEditor}>
                      Cancel edit
                    </Button>
                  )}
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  {INSTRUCTION_FIELDS.map((field) => (
                    <div key={field.key} className="space-y-1.5 sm:col-span-2">
                      <Label htmlFor={`pb-${field.key}`}>{field.label}</Label>
                      <Textarea
                        id={`pb-${field.key}`}
                        placeholder={field.placeholder}
                        value={instructions[field.key] ?? ""}
                        onChange={(ev) => setField(field.key, ev.target.value)}
                      />
                    </div>
                  ))}
                  <div className="space-y-1.5">
                    <Label htmlFor="pb-status">Status</Label>
                    <Select
                      id="pb-status"
                      value={status}
                      onChange={(ev) => setStatus(ev.target.value)}
                    >
                      <option value="active">active</option>
                      <option value="inactive">inactive</option>
                    </Select>
                  </div>
                </div>
                <ErrorAlert error={editorError} />
                <Button type="submit" disabled={editorBusy}>
                  {editorBusy ? <Loader2 className="animate-spin" /> : null}
                  {editingId ? "Save changes" : "Create playbook"}
                </Button>
              </form>
            </>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={pendingDelete !== null}
        onOpenChange={(open) => (open ? undefined : setPendingDelete(null))}
      >
        <DialogContent showCloseButton={!deleteBusy} className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-base font-semibold">Delete playbook?</DialogTitle>
            <DialogDescription>
              This permanently removes the playbook. Calls to this provider will fall back to the
              generic IVR navigator.
            </DialogDescription>
          </DialogHeader>
          <ErrorAlert error={deleteError} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingDelete(null)} disabled={deleteBusy}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmDelete} disabled={deleteBusy}>
              {deleteBusy ? <Loader2 className="animate-spin" /> : <Trash2 />}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
