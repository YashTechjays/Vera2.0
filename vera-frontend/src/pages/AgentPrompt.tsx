import { useCallback, useEffect, useMemo, useState, type JSX } from "react"
import { Bot, Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Select } from "@/components/ui/select"
import { PreviewPane, type PreviewSection } from "@/components/agent-prompt/PreviewPane"
import { SessionEditor } from "@/components/agent-prompt/SessionEditor"
import { TaskOverrideEditor } from "@/components/agent-prompt/TaskOverrideEditor"
import { VersionList } from "@/components/agent-prompt/VersionList"
import { ApiError } from "@/lib/api/client"
import {
  createPromptDraft,
  getPromptSchema,
  getPromptVersion,
  listPromptVersions,
  listPrompts,
  previewPromptDocument,
  previewPromptVersion,
  publishPromptVersion,
  type PromptDocument,
  type PromptSchemaDetail,
  type PromptSummary,
  type PromptVersionSummary,
  type RenderedPrompts,
  type SessionBlock,
} from "@/lib/api/prompts"
import {
  clearOverrideField,
  clientValidationErrors,
  documentsEqual,
  normalizeDocument,
  parsePromptErrors,
  placeholderGroupsOf,
  removeOverrideEntry,
  setOverrideField,
  taskDefaultsOf,
  type OverrideField,
  type ParsedErrors,
} from "@/lib/prompts/document"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"
import { pickInitialVersion } from "@/pages/agentPrompt.helpers"

type Selection = { kind: "session" } | { kind: "task"; taskKey: string }
type PendingAction = { kind: "load"; versionId: string } | { kind: "switch-prompt"; promptId: string }

const NO_ERRORS: ParsedErrors = { fields: {}, general: [] }

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback
}

export function AgentPrompt(): JSX.Element {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [prompts, setPrompts] = useState<PromptSummary[]>([])
  const [promptId, setPromptId] = useState<string | null>(null)
  const [versions, setVersions] = useState<PromptVersionSummary[]>([])
  const [schema, setSchema] = useState<PromptSchemaDetail | null>(null)
  const [doc, setDoc] = useState<PromptDocument | null>(null)
  const [baseline, setBaseline] = useState<PromptDocument | null>(null)
  const [loadedVersionId, setLoadedVersionId] = useState<string | null>(null)
  const [selection, setSelection] = useState<Selection>({ kind: "session" })
  const [preview, setPreview] = useState<RenderedPrompts | null>(null)
  const [previewErrors, setPreviewErrors] = useState<ParsedErrors>(NO_ERRORS)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [saveErrors, setSaveErrors] = useState<ParsedErrors>(NO_ERRORS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [publishingId, setPublishingId] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)

  const tasks = useMemo(() => (schema === null ? [] : taskDefaultsOf(schema.document)), [schema])
  const groups = useMemo(
    () => (schema === null ? { system: [], context: [] } : placeholderGroupsOf(schema.document)),
    [schema],
  )
  const dirty = useMemo(
    () => doc !== null && baseline !== null && !documentsEqual(doc, baseline),
    [doc, baseline],
  )
  const clientErrors = useMemo(() => (doc === null ? {} : clientValidationErrors(doc)), [doc])
  const fieldErrors = useMemo(() => {
    const merged: Record<string, string[]> = { ...clientErrors }
    for (const source of [previewErrors.fields, saveErrors.fields]) {
      for (const [key, messages] of Object.entries(source)) {
        merged[key] = [...(merged[key] ?? []), ...messages]
      }
    }
    return merged
  }, [clientErrors, previewErrors.fields, saveErrors.fields])
  const generalErrors = useMemo(
    () => [...previewErrors.general, ...saveErrors.general],
    [previewErrors.general, saveErrors.general],
  )
  const orphanedKeys = useMemo(() => {
    if (doc === null) return []
    const known = new Set(tasks.map((t) => t.task_key))
    return Object.keys(doc.task_overrides).filter((key) => !known.has(key))
  }, [doc, tasks])
  const loadedVersion = versions.find((v) => v.id === loadedVersionId) ?? null

  const loadVersionIntoBuffer = useCallback(async (pid: string, versionId: string) => {
    const detail = await getPromptVersion(pid, versionId)
    const normalized = normalizeDocument(detail.composite_json)
    setDoc(normalized)
    setBaseline(normalized)
    setLoadedVersionId(versionId)
    setSaveErrors(NO_ERRORS)
  }, [])

  // Load the catalog once.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listPrompts()
      .then((list) => {
        if (cancelled) return
        setPrompts(list)
        setPromptId(list[0]?.id ?? null)
        if (list.length === 0) setLoading(false)
      })
      .catch((err) => {
        if (cancelled) return
        setError(errorMessage(err, "Could not load prompts."))
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  // (Re)load versions + schema + initial buffer when the selected prompt changes.
  useEffect(() => {
    if (promptId === null) return
    let cancelled = false
    // Reset the buffer for the newly selected prompt. Wrapped in a function (rather
    // than called inline) so react-hooks/set-state-in-effect doesn't read these as
    // render-synchronizing setState calls — this batch is a one-time reset, not a
    // subscription to external state.
    function resetForPrompt(): void {
      setLoading(true)
      setError(null)
      setSchema(null)
      setDoc(null)
      setBaseline(null)
      setLoadedVersionId(null)
      setSelection({ kind: "session" })
      setPreview(null)
      setPreviewErrors(NO_ERRORS)
      setPreviewLoading(false)
      setPreviewError(null)
      setSaveErrors(NO_ERRORS)
    }
    resetForPrompt()
    async function bootstrap(pid: string): Promise<void> {
      const [vs, schemaDetail] = await Promise.all([listPromptVersions(pid), getPromptSchema(pid)])
      if (cancelled) return
      setVersions(vs)
      setSchema(schemaDetail)
      const initial = pickInitialVersion(vs)
      if (initial !== undefined) {
        await loadVersionIntoBuffer(pid, initial.id)
        return
      }
      // Bootstrap gap: no versions — seed the session from the factory render.
      const factory = await previewPromptVersion(pid)
      if (cancelled) return
      const seeded: PromptDocument = {
        kind: "prompt_document",
        session: {
          persona: factory.persona,
          goal: factory.goal,
          base_instructions: factory.base_instructions,
        },
        task_overrides: {},
      }
      setDoc(seeded)
      setBaseline(seeded)
    }
    bootstrap(promptId)
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err, "Could not load the prompt."))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [promptId, loadVersionIntoBuffer])

  // Preview: authoritative GET for a pristine loaded version; debounced stateless
  // POST for a dirty buffer (spec §3.4).
  useEffect(() => {
    if (promptId === null || doc === null) return
    let cancelled = false
    // See resetForPrompt() above for why this is wrapped rather than inline.
    function beginPreview(): void {
      setPreviewLoading(true)
      setPreviewError(null)
    }
    beginPreview()
    const buffer = doc

    async function renderPristine(pid: string, versionId: string): Promise<void> {
      const rendered = await previewPromptVersion(pid, versionId)
      if (cancelled) return
      setPreview(rendered)
      setPreviewErrors(NO_ERRORS)
    }
    async function renderBuffer(pid: string): Promise<void> {
      const result = await previewPromptDocument(pid, normalizeDocument(buffer))
      if (cancelled) return
      setPreview(result.rendered)
      setPreviewErrors(parsePromptErrors(result.errors.join("; ")))
    }

    function run(task: Promise<void>): void {
      task
        .catch((err) => {
          if (!cancelled) setPreviewError(errorMessage(err, "Could not render the preview."))
        })
        .finally(() => {
          if (!cancelled) setPreviewLoading(false)
        })
    }

    // A pristine loaded version (unmodified, has a version id) uses the authoritative
    // GET; anything else is a dirty buffer rendered via the debounced POST below.
    if (!dirty && loadedVersionId !== null) {
      run(renderPristine(promptId, loadedVersionId))
      return () => {
        cancelled = true
      }
    }
    const timer = setTimeout(() => run(renderBuffer(promptId)), 500)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [promptId, doc, dirty, loadedVersionId])

  if (!isSuperAdmin) {
    return <p className="text-sm text-muted-foreground">This page is only available to platform operators.</p>
  }

  async function refreshVersions(pid: string): Promise<void> {
    setVersions(await listPromptVersions(pid))
  }

  async function onSave(): Promise<void> {
    if (promptId === null || doc === null || Object.keys(clientErrors).length > 0) return
    setBusy(true)
    setError(null)
    try {
      const created = await createPromptDraft(promptId, normalizeDocument(doc))
      // Refresh the version list before pointing loadedVersionId at the new draft, so
      // `loadedVersion` (looked up by id in `versions`) resolves on the same render
      // instead of momentarily missing and flashing the "unsaved changes" caption.
      await refreshVersions(promptId)
      const normalized = normalizeDocument(created.composite_json)
      setDoc(normalized)
      setBaseline(normalized)
      setLoadedVersionId(created.id)
      setSaveErrors(NO_ERRORS)
    } catch (err) {
      if (err instanceof ApiError && err.httpStatus === 400) {
        setSaveErrors(parsePromptErrors(err.message))
      } else {
        setError(errorMessage(err, "Could not save the draft."))
      }
    } finally {
      setBusy(false)
    }
  }

  async function onPublish(versionId: string): Promise<void> {
    if (promptId === null) return
    setPublishingId(versionId)
    setError(null)
    try {
      await publishPromptVersion(promptId, versionId)
      await refreshVersions(promptId)
    } catch (err) {
      setError(errorMessage(err, "Could not publish."))
    } finally {
      setPublishingId(null)
    }
  }

  function onLoadRequest(versionId: string): void {
    if (dirty) {
      setPendingAction({ kind: "load", versionId })
      return
    }
    void onLoadConfirmed(versionId)
  }

  async function onLoadConfirmed(versionId: string): Promise<void> {
    if (promptId === null) return
    setPendingAction(null)
    setError(null)
    try {
      await loadVersionIntoBuffer(promptId, versionId)
    } catch (err) {
      setError(errorMessage(err, "Could not load the version."))
    }
  }

  function onPromptSelect(nextPromptId: string): void {
    if (nextPromptId === promptId) return
    if (dirty) {
      setPendingAction({ kind: "switch-prompt", promptId: nextPromptId })
      return
    }
    setPromptId(nextPromptId)
  }

  function onPendingActionConfirmed(): void {
    if (pendingAction === null) return
    if (pendingAction.kind === "load") {
      void onLoadConfirmed(pendingAction.versionId)
      return
    }
    setPendingAction(null)
    setPromptId(pendingAction.promptId)
  }

  function onSessionChange(field: keyof SessionBlock, text: string): void {
    if (doc === null) return
    setDoc({ ...doc, session: { ...doc.session, [field]: text } })
  }

  function onOverrideSet(taskKey: string, field: OverrideField, text: string): void {
    if (doc === null) return
    setDoc(setOverrideField(doc, taskKey, field, text))
  }

  function onOverrideClear(taskKey: string, field: OverrideField): void {
    if (doc === null) return
    setDoc(clearOverrideField(doc, taskKey, field))
  }

  const selectedTask =
    selection.kind === "task" ? (tasks.find((t) => t.task_key === selection.taskKey) ?? null) : null
  const previewTask =
    selection.kind === "task"
      ? (preview?.tasks.find((t) => t.task_key === selection.taskKey) ?? null)
      : null
  const previewSections: PreviewSection[] =
    selection.kind === "session"
      ? [
          { label: "Persona", text: preview?.persona ?? "" },
          { label: "Goal", text: preview?.goal ?? "" },
          { label: "Base instructions", text: preview?.base_instructions ?? "" },
        ]
      : [
          { label: "Intro (spoken on entry)", text: previewTask?.intro ?? "— none —" },
          { label: "Compiled instructions", text: previewTask?.prompt ?? "" },
          { label: "Outro (spoken on exit)", text: previewTask?.outro ?? "— none —" },
        ]
  const previewMeta =
    !dirty && loadedVersion !== null
      ? `v${loadedVersion.version} · pinned schema v${loadedVersion.schema_version}`
      : `unsaved changes · renders against schema v${schema?.version ?? "?"} (published)`
  const saveDisabled = busy || !dirty || Object.keys(clientErrors).length > 0 || schema === null

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Agent Prompt</h1>
          <p className="text-sm text-muted-foreground">
            Session text and per-task overrides; prompts render from the schema at call time.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {prompts.length > 1 && (
            <div className="w-56">
              <Select value={promptId ?? ""} onChange={(e) => onPromptSelect(e.target.value)}>
                {prompts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </Select>
            </div>
          )}
          {dirty && <Badge variant="secondary">Unsaved changes</Badge>}
          <Button type="button" onClick={() => void onSave()} disabled={saveDisabled}>
            {busy ? <Loader2 className="animate-spin" /> : null} Save draft
          </Button>
        </div>
      </div>

      {error !== null && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {generalErrors.map((message) => (
        <Alert key={message} variant="destructive">
          <AlertDescription>{message}</AlertDescription>
        </Alert>
      ))}

      {loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </p>
      ) : promptId === null ? (
        <p className="text-sm text-muted-foreground">No prompts found.</p>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[230px_minmax(0,1fr)_minmax(0,1fr)]">
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Bot className="size-4 text-muted-foreground" /> Sections
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-1">
                <button
                  type="button"
                  onClick={() => setSelection({ kind: "session" })}
                  className={
                    selection.kind === "session"
                      ? "w-full rounded-md bg-muted px-2 py-1.5 text-left text-sm font-medium"
                      : "w-full rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
                  }
                >
                  Session
                </button>
                <p className="px-2 pt-2 text-xs font-medium text-muted-foreground">Tasks</p>
                {tasks.map((task) => {
                  const active = selection.kind === "task" && selection.taskKey === task.task_key
                  const overridden = doc !== null && task.task_key in doc.task_overrides
                  return (
                    <button
                      key={task.task_key}
                      type="button"
                      onClick={() => setSelection({ kind: "task", taskKey: task.task_key })}
                      className={
                        active
                          ? "flex w-full items-center justify-between rounded-md bg-muted px-2 py-1.5 text-left text-sm font-medium"
                          : "flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
                      }
                    >
                      <span className="truncate">{task.title}</span>
                      {overridden && <span className="size-1.5 shrink-0 rounded-full bg-primary" />}
                    </button>
                  )
                })}
                {orphanedKeys.map((key) => (
                  <div
                    key={key}
                    className="flex items-center justify-between gap-2 rounded-md border border-destructive/50 px-2 py-1.5"
                  >
                    <span className="truncate text-xs text-destructive">
                      {key}: override for a task not in the published schema
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => doc !== null && setDoc(removeOverrideEntry(doc, key))}
                    >
                      Remove
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Versions</CardTitle>
              </CardHeader>
              <CardContent>
                <VersionList
                  versions={versions}
                  loadedVersionId={loadedVersionId}
                  busy={busy || publishingId !== null}
                  publishingId={publishingId}
                  onLoad={onLoadRequest}
                  onPublish={(id) => void onPublish(id)}
                />
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                {selection.kind === "session" ? "Session" : (selectedTask?.title ?? "Task")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {doc !== null && selection.kind === "session" && (
                <SessionEditor
                  session={doc.session}
                  errors={fieldErrors}
                  groups={groups}
                  onChange={onSessionChange}
                />
              )}
              {doc !== null && selectedTask !== null && (
                <TaskOverrideEditor
                  task={selectedTask}
                  document={doc}
                  errors={fieldErrors}
                  groups={groups}
                  onSet={(field, text) => onOverrideSet(selectedTask.task_key, field, text)}
                  onClear={(field) => onOverrideClear(selectedTask.task_key, field)}
                />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="pt-6">
              <PreviewPane
                title={selection.kind === "session" ? "Session text" : (selectedTask?.title ?? "")}
                meta={previewMeta}
                loading={previewLoading}
                error={previewError}
                sections={previewSections}
              />
            </CardContent>
          </Card>
        </div>
      )}

      <Dialog open={pendingAction !== null} onOpenChange={(open) => !open && setPendingAction(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard unsaved changes?</DialogTitle>
            <DialogDescription>
              Switching away replaces your unsaved edits. Save a draft first if you want to keep
              them.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPendingAction(null)}>
              Keep editing
            </Button>
            <Button type="button" variant="destructive" onClick={onPendingActionConfirmed}>
              Discard and load
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
