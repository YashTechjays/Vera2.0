import { useCallback, useEffect, useState, type FormEvent } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  RichSelect,
  RichSelectContent,
  RichSelectItem,
  RichSelectTrigger,
  RichSelectValue,
} from "@/components/ui/rich-select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import {
  createApiKey,
  listApiKeyScopes,
  listApiKeys,
  revokeApiKey,
  type ApiKey,
  type ApiKeyScope,
  type CreatedApiKey,
} from "@/lib/api-keys"
import { formatDate } from "@/lib/patient-forms/display"

/** Manage the tenant's inbound API keys: list, create (one-time token), revoke.
 *  Mount only behind an `apikeys:manage` check — the endpoints are gated server-side too. */
export function ApiKeysSection() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [scopes, setScopes] = useState<ApiKeyScope[]>([])
  const [error, setError] = useState<string | null>(null)

  const [name, setName] = useState("")
  const [scope, setScope] = useState("")
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [created, setCreated] = useState<CreatedApiKey | null>(null)
  const [copied, setCopied] = useState(false)
  const [revokingId, setRevokingId] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setKeys(await listApiKeys())
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load API keys.")
      setKeys([])
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    listApiKeys()
      .then((rows) => {
        if (!cancelled) {
          setKeys(rows)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load API keys.")
          setKeys([])
        }
      })
    listApiKeyScopes()
      .then((s) => {
        if (!cancelled) {
          setScopes(s)
          if (s.length) setScope((cur) => cur || s[0].code)
        }
      })
      .catch(() => {
        if (!cancelled) setScopes([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleCreate = useCallback(
    async (e: FormEvent) => {
      e.preventDefault()
      if (!name.trim() || !scope) return
      setCreating(true)
      setCreateError(null)
      setCreated(null)
      setCopied(false)
      try {
        const key = await createApiKey(name.trim(), scope)
        setCreated(key)
        setName("")
        await refresh()
      } catch (err) {
        // 409 when an active key already has this name (server unique constraint).
        setCreateError(err instanceof ApiError ? err.message : "Could not create the key.")
      } finally {
        setCreating(false)
      }
    },
    [name, scope, refresh],
  )

  const handleRevoke = useCallback(
    async (k: ApiKey) => {
      if (!window.confirm(`Revoke API key "${k.name}"? Systems using it will stop working.`)) return
      setRevokingId(k.id)
      try {
        await revokeApiKey(k.id)
        await refresh()
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not revoke the key.")
      } finally {
        setRevokingId(null)
      }
    },
    [refresh],
  )

  const copyToken = useCallback(() => {
    if (!created) return
    void navigator.clipboard.writeText(created.token).then(() => setCopied(true))
  }, [created])

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-sm font-medium">API Keys</h2>
        <p className="text-sm text-muted-foreground">
          Inbound keys external systems use to send data to Vera. The token is shown only once,
          right after you create the key.
        </p>
      </div>

      {/* One-time token reveal — never retrievable again. */}
      {created && (
        <div className="space-y-2 rounded-lg border border-emerald-300 bg-emerald-50 p-3">
          <p className="text-sm font-medium text-emerald-800">
            Key “{created.name}” created — copy the token now, it won’t be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 truncate rounded border bg-white px-2 py-1 font-mono text-xs">
              {created.token}
            </code>
            <Button type="button" size="sm" onClick={copyToken}>
              {copied ? "Copied" : "Copy token"}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setCreated(null)}>
              Done
            </Button>
          </div>
        </div>
      )}

      {/* Create form — scope is a dropdown of the backend-owned vocabulary. */}
      <form className="flex flex-wrap items-end gap-2" onSubmit={handleCreate}>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">Name</label>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Intake sheet"
            className="w-56"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">Scope</label>
          <RichSelect value={scope} onValueChange={setScope} disabled={scopes.length === 0}>
            <RichSelectTrigger className="w-72">
              <RichSelectValue
                placeholder={scopes.length === 0 ? "No scopes available" : "Select a scope"}
              />
            </RichSelectTrigger>
            <RichSelectContent>
              {scopes.map((s) => (
                <RichSelectItem key={s.code} value={s.code} caption={s.description}>
                  <span className="font-mono">{s.code}</span>
                </RichSelectItem>
              ))}
            </RichSelectContent>
          </RichSelect>
        </div>
        <Button type="submit" disabled={creating || !name.trim() || !scope}>
          {creating ? "Creating…" : "Create key"}
        </Button>
      </form>
      {createError && (
        <p className="text-sm text-destructive" role="alert">
          {createError}
        </p>
      )}
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Name</TableHead>
              <TableHead>Scope</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys === null && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {keys?.length === 0 && !error && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  No API keys yet.
                </TableCell>
              </TableRow>
            )}
            {keys?.map((k) => (
              <TableRow key={k.id}>
                <TableCell className="font-medium">{k.name}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{k.scope}</TableCell>
                <TableCell className="text-muted-foreground">
                  {k.expires_at ? formatDate(k.expires_at) : "Never"}
                </TableCell>
                <TableCell>
                  <span
                    className={cn(
                      "inline-block rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
                      k.revoked ? "bg-red-100 text-red-700" : "bg-emerald-100 text-emerald-700",
                    )}
                  >
                    {k.revoked ? "Revoked" : "Active"}
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  {!k.revoked && (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={revokingId === k.id}
                      onClick={() => handleRevoke(k)}
                    >
                      {revokingId === k.id ? "Revoking…" : "Revoke"}
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  )
}
