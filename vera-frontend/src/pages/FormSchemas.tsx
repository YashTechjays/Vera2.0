import { useEffect, useState } from "react"
import { Check, Copy } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { ApiError } from "@/lib/api/client"
import { copyText } from "@/lib/clipboard"
import {
  listFormSchemas,
  listSchemaVersions,
  type FormSchemaSummary,
  type SchemaVersionSummary,
} from "@/lib/api/formSchemas"
import { usePermission } from "@/lib/auth/permissions"
import { useAppSelector } from "@/store/hooks"
import { selectIsSuperAdmin } from "@/store/authSlice"

function formatDate(iso: string | null): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
}

/** "infertility_treatment" → "Infertility treatment". */
function humanize(value: string): string {
  const spaced = value.replaceAll("_", " ")
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

/** A UUID the intake API needs (form_type_id / schema_version_id): shown in
 *  full mono with a copy button that ticks briefly on success. */
function CopyableId({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <span className="inline-flex items-center gap-1">
      <code className="font-mono text-xs text-muted-foreground">{value}</code>
      <button
        type="button"
        title={copied ? "Copied" : `Copy ${label}`}
        aria-label={copied ? "Copied" : `Copy ${label}`}
        className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
        onClick={() => {
          void copyText(value).then((ok) => {
            if (!ok) return
            setCopied(true)
            setTimeout(() => setCopied(false), 2000)
          })
        }}
      >
        {copied ? <Check className="size-3.5 text-emerald-600" /> : <Copy className="size-3.5" />}
      </button>
    </span>
  )
}

/** Read-only Super Admin catalog of form schemas; View lists a schema's
 *  versions with the active (published) one highlighted. */
export function FormSchemas() {
  const isSuperAdmin = useAppSelector(selectIsSuperAdmin)
  const canRead = usePermission("platform:form_schemas:read")
  const [schemas, setSchemas] = useState<FormSchemaSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Versions dialog.
  const [viewing, setViewing] = useState<FormSchemaSummary | null>(null)
  const [versions, setVersions] = useState<SchemaVersionSummary[] | null>(null)
  const [dialogError, setDialogError] = useState<string | null>(null)

  useEffect(() => {
    if (!isSuperAdmin) return
    let cancelled = false
    listFormSchemas()
      .then((s) => {
        if (!cancelled) setSchemas(s)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : "Could not load form schemas.")
        setSchemas((prev) => prev ?? []) // else the table sticks on "Loading…"
      })
    return () => {
      cancelled = true
    }
  }, [isSuperAdmin])

  function openView(schema: FormSchemaSummary) {
    setViewing(schema)
    setVersions(null)
    setDialogError(null)
    listSchemaVersions(schema.id)
      .then(setVersions)
      .catch((err) => {
        setDialogError(err instanceof ApiError ? err.message : "Could not load versions.")
        setVersions([])
      })
  }

  function renderVersions() {
    if (versions === null) {
      return <p className="py-4 text-sm text-muted-foreground">Loading…</p>
    }
    if (versions.length === 0 && !dialogError) {
      return <p className="py-4 text-sm text-muted-foreground">No versions yet.</p>
    }
    return (
      <ul className="space-y-2">
        {versions.map((v) => {
          const active = v.status === "published"
          return (
            <li
              key={v.id}
              className={`space-y-1.5 rounded-md border p-3 ${
                active ? "border-emerald-500 bg-emerald-50" : "border-border"
              }`}
            >
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-semibold">v{v.version}</span>
                  {active ? (
                    <Badge className="bg-emerald-600 hover:bg-emerald-600">Active</Badge>
                  ) : (
                    <Badge variant="secondary" className="capitalize">{v.status}</Badge>
                  )}
                </div>
                <span className="text-xs text-muted-foreground">
                  {active && v.published_at
                    ? `Published ${formatDate(v.published_at)}`
                    : `Created ${formatDate(v.created_at)}`}
                </span>
              </div>
              <div className="text-xs text-muted-foreground">
                Schema version ID: <CopyableId value={v.id} label="schema version ID" />
              </div>
            </li>
          )
        })}
      </ul>
    )
  }

  if (!isSuperAdmin) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Form Schemas</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          This catalog is managed by platform operators only.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Form Schemas</h1>
        <p className="text-sm text-muted-foreground">
          {schemas ? `${schemas.length} schema${schemas.length === 1 ? "" : "s"}` : "Loading…"}
        </p>
      </div>

      {error && <p className="text-sm text-destructive" role="alert">{error}</p>}

      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Insurance type</TableHead>
              <TableHead>Form schema ID</TableHead>
              <TableHead>Active version</TableHead>
              <TableHead>Versions</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {schemas === null && (
              <TableRow>
                <TableCell colSpan={7} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {schemas?.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="py-6 text-center text-muted-foreground">
                  No form schemas yet.
                </TableCell>
              </TableRow>
            )}
            {schemas?.map((s) => (
              <TableRow key={s.id}>
                <TableCell className="font-medium">{s.name}</TableCell>
                <TableCell className="text-muted-foreground">
                  {humanize(s.insurance_type)}
                </TableCell>
                <TableCell>
                  <CopyableId value={s.id} label="form schema ID" />
                </TableCell>
                <TableCell>
                  {s.active_version !== null ? (
                    <Badge>v{s.active_version}</Badge>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-muted-foreground">{s.version_count}</TableCell>
                <TableCell className="text-muted-foreground">{formatDate(s.created_at)}</TableCell>
                <TableCell>
                  {canRead && (
                    <Button variant="ghost" size="sm" className="-ml-2.5" onClick={() => openView(s)}>
                      View
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Versions dialog — the published version is the active schema. */}
      <Dialog open={viewing !== null} onOpenChange={(o) => (o ? undefined : setViewing(null))}>
        <DialogContent className="max-w-lg gap-0 p-0">
          <DialogHeader className="p-5">
            <DialogTitle className="text-base font-semibold">
              {viewing ? `${viewing.name} — versions` : "Versions"}
            </DialogTitle>
            <DialogDescription>
              The published version is the active schema used for new intakes. In an intake
              payload, the form schema ID goes in form_type_id and the version ID in
              schema_version_id.
            </DialogDescription>
          </DialogHeader>
          {dialogError && (
            <p className="px-5 pb-2 text-sm text-destructive" role="alert">{dialogError}</p>
          )}
          <div className="max-h-[50vh] overflow-auto px-5 pb-5">{renderVersions()}</div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
