import { useCallback, useEffect, useState } from "react"
import { ArrowDown, ArrowUp, Search } from "lucide-react"

import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
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
import { usePermission } from "@/lib/auth/permissions"
import { useIbv } from "@/components/ibv/IbvProvider"
import { listPatientForms } from "@/lib/patient-forms/api"
import type {
  PatientFormSortKey,
  PatientFormStatus,
  PatientFormSummary,
} from "@/lib/patient-forms/types"
import { formatDate, statusBadgeClass, statusLabel } from "@/lib/patient-forms/display"

const PAGE_SIZE = 20

type TabKey = "all" | "completed"
const TABS: { key: TabKey; label: string }[] = [
  { key: "all", label: "All Data" },
  { key: "completed", label: "Completed" },
]

const STATUS_OPTIONS: PatientFormStatus[] = [
  "ready_for_processing",
  "in_queue",
  "in_call",
  "ai_processing",
  "exception_review",
  "completed",
  "call_failed",
]

type SortKey = Exclude<PatientFormSortKey, "created_at">
const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "appointment_date", label: "Appointment Date" },
  { key: "appointment_type", label: "Appointment Type" },
  { key: "patient_name", label: "Patient Name" },
  { key: "member_id", label: "Member/Policy ID" },
  { key: "insurance_provider", label: "Insurance Provider" },
  { key: "status", label: "Status" },
]

export function DataManagement() {
  const canRead = usePermission("forms:read")
  const { openFormById, savedTick } = useIbv()

  const [items, setItems] = useState<PatientFormSummary[] | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [tab, setTab] = useState<TabKey>("all")
  const [status, setStatus] = useState<"" | PatientFormStatus>("")
  const [query, setQuery] = useState("")
  // Most recent appointment first by default; the server sorts the full set.
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "appointment_date",
    dir: "desc",
  })
  const [error, setError] = useState<string | null>(null)
  // Periodic tick so the worklist reflects server-side status changes (a call
  // ending → post-call eval → completed/exception_review) without the user having
  // to reload. Without this the list is a frozen snapshot from page load.
  const [autoTick, setAutoTick] = useState(0)

  // Tab "Completed" forces a status filter; otherwise the Select drives it.
  const effectiveStatus = tab === "completed" ? "completed" : status || undefined

  useEffect(() => {
    const id = setInterval(() => setAutoTick((n) => n + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (!canRead) return
    let cancelled = false
    listPatientForms({
      page,
      page_size: PAGE_SIZE,
      status: effectiveStatus,
      q: query.trim() || undefined,
      sort_by: sort.key,
      sort_dir: sort.dir,
    })
      .then((res) => {
        if (cancelled) return
        setItems(res.items)
        setTotal(res.total)
        setError(null)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load patient forms.")
          setItems([])
        }
      })
    return () => {
      cancelled = true
    }
  }, [canRead, page, effectiveStatus, query, sort, savedTick, autoTick])

  const toggleSort = useCallback((key: SortKey) => {
    setSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" },
    )
    setPage(1)
  }, [])

  if (!canRead) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Data Management</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          You don't have permission to view patient forms.
        </p>
      </div>
    )
  }

  const rows = items ?? []
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Data Management</h1>

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-4 px-4">
          <div className="flex items-center gap-1 rounded-lg bg-muted p-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => {
                  setTab(t.key)
                  setPage(1)
                }}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  tab === t.key
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3">
            <form
              className="relative w-56"
              onSubmit={(e) => {
                e.preventDefault()
                setPage(1)
              }}
            >
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search patient name"
                className="pl-8"
              />
            </form>
            <div className="w-44">
              <Select
                value={status}
                disabled={tab === "completed"}
                onChange={(e) => {
                  setStatus(e.target.value as "" | PatientFormStatus)
                  setPage(1)
                }}
              >
                <option value="">All Status</option>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {statusLabel(s)}
                  </option>
                ))}
              </Select>
            </div>
          </div>
        </div>

        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              {COLUMNS.map((col) => (
                <TableHead
                  key={col.key}
                  className={cn(
                    "cursor-pointer select-none",
                    col.key === "appointment_date" && "pl-6",
                  )}
                  onClick={() => toggleSort(col.key)}
                >
                  <span className="flex items-center gap-1.5">
                    {col.label}
                    {sort.key === col.key && sort.dir === "desc" ? (
                      <ArrowUp className="size-3.5 text-muted-foreground" />
                    ) : (
                      <ArrowDown
                        className={cn(
                          "size-3.5",
                          sort.key === col.key
                            ? "text-foreground"
                            : "text-muted-foreground/50",
                        )}
                      />
                    )}
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {items === null && (
              <TableRow>
                <TableCell
                  colSpan={COLUMNS.length}
                  className="py-10 text-center text-muted-foreground"
                >
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {items?.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={COLUMNS.length}
                  className="py-10 text-center text-muted-foreground"
                >
                  No records match your filters.
                </TableCell>
              </TableRow>
            )}
            {rows.map((f) => (
              <TableRow
                key={f.id}
                className="cursor-pointer"
                onClick={() => openFormById(f.id)}
              >
                <TableCell className="pl-6">{formatDate(f.appointment_date)}</TableCell>
                <TableCell className="text-muted-foreground">
                  {f.appointment_type || "—"}
                </TableCell>
                <TableCell className="font-medium capitalize">{f.patient_name || "—"}</TableCell>
                <TableCell className="text-muted-foreground">
                  {f.member_id || "—"}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {f.insurance_provider || "—"}
                </TableCell>
                <TableCell>
                  <span
                    className={cn(
                      "inline-block rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide",
                      statusBadgeClass(f.status),
                    )}
                  >
                    {statusLabel(f.status)}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {error && (
          <p className="px-4 py-3 text-sm text-destructive" role="alert">
            {error}
          </p>
        )}

        <div className="flex items-center justify-between gap-4 px-4 py-3">
          <span className="text-sm text-muted-foreground">
            {items
              ? `${total} form${total === 1 ? "" : "s"} · page ${page} of ${lastPage}`
              : "Loading…"}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= lastPage}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}
