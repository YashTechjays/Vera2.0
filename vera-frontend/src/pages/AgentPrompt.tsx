import { useCallback, useEffect, useState, type FormEvent, type JSX } from "react"
import { Bot, Loader2 } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/client"
import {
  createPromptDraft,
  getPromptVersion,
  listPromptVersions,
  listPrompts,
  publishPromptVersion,
  type CompositeJson,
  type PromptSummary,
  type PromptVersionSummary,
} from "@/lib/api/prompts"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"
import { pickInitialVersion } from "@/pages/agentPrompt.helpers"

export function AgentPrompt(): JSX.Element {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [prompt, setPrompt] = useState<PromptSummary | null>(null)
  const [versions, setVersions] = useState<PromptVersionSummary[]>([])
  const [composite, setComposite] = useState<CompositeJson | null>(null)
  const [text, setText] = useState("")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<"save" | "publish" | null>(null)

  const loadVersionInto = useCallback(async (promptId: string, versionId: string) => {
    const detail = await getPromptVersion(promptId, versionId)
    setComposite(detail.composite_json)
    setText(detail.composite_json.prompt ?? "")
  }, [])

  const refresh = useCallback(async (promptId: string) => {
    const vs = await listPromptVersions(promptId)
    setVersions(vs)
    const current = pickInitialVersion(vs)
    if (current) await loadVersionInto(promptId, current.id)
  }, [loadVersionInto])

  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listPrompts()
      .then(async (prompts) => {
        if (cancelled) return
        const first = prompts[0] ?? null
        setPrompt(first)
        if (first) await refresh(first.id)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load prompts.")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin, refresh])

  if (!isSuperAdmin) {
    return <p className="text-sm text-muted-foreground">This page is only available to platform operators.</p>
  }

  async function onSaveDraft(e: FormEvent): Promise<void> {
    e.preventDefault()
    if (!prompt || !composite) return
    setBusy("save")
    setError(null)
    try {
      await createPromptDraft(prompt.id, { ...composite, prompt: text })
      await refresh(prompt.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the draft.")
    } finally {
      setBusy(null)
    }
  }

  async function onPublish(versionId: string): Promise<void> {
    if (!prompt) return
    setBusy("publish")
    setError(null)
    try {
      await publishPromptVersion(prompt.id, versionId)
      await refresh(prompt.id)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not publish.")
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Agent Prompt</h1>
        <p className="text-sm text-muted-foreground">
          Edit the agent prompt, save it as a draft, and publish a version.
        </p>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </p>
      ) : !prompt ? (
        <p className="text-sm text-muted-foreground">No prompts found.</p>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Bot className="size-4 text-muted-foreground" />
                {prompt.name}
              </CardTitle>
              <CardDescription>{prompt.insurance_type}</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={onSaveDraft} className="space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor="prompt-text">Prompt</Label>
                  <Textarea
                    id="prompt-text"
                    className="min-h-80 font-mono text-xs"
                    value={text}
                    onChange={(ev) => setText(ev.target.value)}
                  />
                </div>
                <Button type="submit" disabled={busy !== null}>
                  {busy === "save" ? <Loader2 className="animate-spin" /> : null} Save as draft
                </Button>
              </form>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Versions</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {versions.map((v) => (
                <div key={v.id} className="flex items-center justify-between gap-4 rounded-md border p-3">
                  <div className="flex items-center gap-3">
                    <span className="text-sm font-medium">v{v.version}</span>
                    <Badge variant={v.status === "published" ? "default" : "secondary"}>{v.status}</Badge>
                  </div>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => loadVersionInto(prompt.id, v.id)} disabled={busy !== null}>
                      View
                    </Button>
                    {v.status !== "published" && (
                      <Button size="sm" onClick={() => onPublish(v.id)} disabled={busy !== null}>
                        {busy === "publish" ? <Loader2 className="animate-spin" /> : null} Publish
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
