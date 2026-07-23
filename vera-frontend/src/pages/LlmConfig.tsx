import { useCallback, useEffect, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { ApiError } from "@/lib/api/client"
import {
  getLlmConfig,
  getLlmConfigHistory,
  resetLlmConfig,
  saveLlmConfig,
  type LlmConfigState,
} from "@/lib/api/llmConfig"
import {
  SUGGESTED_MODELS, canReset, formatUpdatedAt, hasPendingChange,
} from "@/pages/llmConfig.helpers"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

export function LlmConfig() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const [current, setCurrent] = useState<LlmConfigState | null>(null)
  const [history, setHistory] = useState<LlmConfigState[] | null>(null)
  const [input, setInput] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Refresh after a mutation.
  const load = useCallback(async () => {
    setError(null)
    try {
      const [cfg, hist] = await Promise.all([getLlmConfig(), getLlmConfigHistory()])
      setCurrent(cfg)
      setInput(cfg.model ?? "")
      setHistory(hist)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the LLM model config.")
      setHistory((prev) => prev ?? [])
    }
  }, [])

  // Initial load — cancelled-flag idiom, matching InsuranceProviders.tsx / IvrPlaybooks.tsx.
  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    Promise.all([getLlmConfig(), getLlmConfigHistory()])
      .then(([cfg, hist]) => {
        if (cancelled) return
        setCurrent(cfg)
        setInput(cfg.model ?? "")
        setHistory(hist)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : "Could not load the LLM model config.")
        setHistory((prev) => prev ?? [])
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  async function onSave() {
    setError(null)
    setBusy(true)
    try {
      await saveLlmConfig(input)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the model override.")
    } finally {
      setBusy(false)
    }
  }

  async function onReset() {
    setError(null)
    setBusy(true)
    try {
      await resetLlmConfig()
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reset the model override.")
    } finally {
      setBusy(false)
    }
  }

  if (!isSuperAdmin) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Voice Model</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          This setting is managed by platform operators only.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Voice Model</h1>
        <p className="text-sm text-muted-foreground">
          Overrides the Gemini model the voice cascade's LLM stage uses, platform-wide.
          Applies to calls dispatched after saving — in-flight calls are unaffected.
        </p>
      </div>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      {current && (
        <div className="space-y-4 rounded-lg border border-border p-4">
          <div className="flex items-center gap-2">
            <span className="text-sm text-muted-foreground">Current:</span>
            <Badge variant={current.is_default ? "outline" : "default"}>
              {current.is_default ? "Default" : "Override"}
            </Badge>
            <span className="font-mono text-sm">
              {current.model ?? "gemini-2.5-flash (hardcoded default)"}
            </span>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="llm-model-input">Model name</Label>
            <Input
              id="llm-model-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="e.g. gemini-3.5-flash"
            />
          </div>

          <div className="flex flex-wrap gap-2">
            {SUGGESTED_MODELS.map((m) => (
              <Button
                key={m}
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setInput(m)}
              >
                {m}
              </Button>
            ))}
          </div>

          <div className="flex gap-3">
            <Button
              onClick={onSave}
              disabled={busy || !hasPendingChange(input, current)}
              className="min-w-[100px]"
            >
              {busy ? "Saving…" : "Save"}
            </Button>
            <Button variant="outline" onClick={onReset} disabled={busy || !canReset(current)}>
              Reset to default
            </Button>
          </div>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-sm font-semibold text-muted-foreground">History</h2>
        <div className="rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Changed</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {history === null && (
                <TableRow>
                  <TableCell colSpan={2} className="py-6 text-center text-muted-foreground">
                    Loading…
                  </TableCell>
                </TableRow>
              )}
              {history?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={2} className="py-6 text-center text-muted-foreground">
                    No changes yet.
                  </TableCell>
                </TableRow>
              )}
              {history?.map((row, i) => (
                <TableRow key={`${row.created_at}-${i}`}>
                  <TableCell className="font-mono text-sm">
                    {row.model ?? "Reset to default"}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {formatUpdatedAt(row.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
