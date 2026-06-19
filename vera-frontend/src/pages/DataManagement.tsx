import { useMemo, useState } from "react"
import { ArrowDown, ArrowUp, Search } from "lucide-react"

import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { RecordFormModal } from "@/components/data-management/RecordFormModal"
import {
  patientForms,
  patientStatusStyles,
  type PatientForm,
  type PatientFormStatus,
} from "@/lib/mock-data"

type TabKey = "all" | "completed"

const TABS: { key: TabKey; label: string }[] = [
  { key: "all", label: "All Data" },
  { key: "completed", label: "Completed" },
]

const STATUS_OPTIONS: PatientFormStatus[] = [
  "READY FOR PROCESSING",
  "IN QUEUE",
  "IN CALL",
  "AI PROCESSING",
  "EXCEPTION REVIEW",
  "COMPLETED",
]

type SortKey = keyof Pick<
  PatientForm,
  | "appointmentDate"
  | "appointmentType"
  | "chartNo"
  | "patientName"
  | "memberPolicyId"
  | "insuranceProvider"
  | "status"
>

/** "MM/DD/YYYY" → "YYYYMMDD" so dates sort chronologically as strings. */
function toSortableDate(mdy: string): string {
  const parts = mdy.split("/")
  if (parts.length !== 3) return mdy
  const [m, d, y] = parts
  return `${y}${m}${d}`
}

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "appointmentDate", label: "Appointment Date" },
  { key: "appointmentType", label: "Appointment Type" },
  { key: "chartNo", label: "Chart No" },
  { key: "patientName", label: "Patient Name" },
  { key: "memberPolicyId", label: "Member/Policy ID" },
  { key: "insuranceProvider", label: "Insurance Provider" },
  { key: "status", label: "Status" },
]

export function DataManagement() {
  const [forms, setForms] = useState<PatientForm[]>(patientForms)
  const [tab, setTab] = useState<TabKey>("all")
  const [query, setQuery] = useState("")
  const [statusFilter, setStatusFilter] = useState<"" | PatientFormStatus>("")
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "appointmentDate",
    dir: "asc",
  })
  const [selected, setSelected] = useState<PatientForm | null>(null)
  const [modalOpen, setModalOpen] = useState(false)

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const filtered = forms.filter((f) => {
      if (tab === "completed" && f.status !== "COMPLETED") return false
      if (statusFilter && f.status !== statusFilter) return false
      if (
        q &&
        ![
          f.patientName,
          f.chartNo,
          f.memberPolicyId,
          f.insuranceProvider,
          f.appointmentType,
        ]
          .join(" ")
          .toLowerCase()
          .includes(q)
      )
        return false
      return true
    })
    const sorted = [...filtered].sort((a, b) => {
      const cmp =
        sort.key === "appointmentDate"
          ? toSortableDate(a.appointmentDate).localeCompare(
              toSortableDate(b.appointmentDate)
            )
          : a[sort.key].localeCompare(b[sort.key])
      return sort.dir === "asc" ? cmp : -cmp
    })
    return sorted
  }, [forms, tab, query, statusFilter, sort])

  const toggleSort = (key: SortKey) =>
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: "asc" }
    )

  const openRecord = (record: PatientForm) => {
    setSelected(record)
    setModalOpen(true)
  }

  const handleStatusChange = (id: string, status: PatientFormStatus) => {
    setForms((prev) => prev.map((f) => (f.id === id ? { ...f, status } : f)))
    setSelected((prev) => (prev && prev.id === id ? { ...prev, status } : prev))
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Data Management</h1>

      <Card>
        {/* Controls */}
        <div className="flex flex-wrap items-center justify-between gap-4 px-4">
          <div className="flex items-center gap-1 rounded-lg bg-muted p-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  tab === t.key
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-3">
            <div className="relative w-56">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search data"
                className="pl-8"
              />
            </div>
            <div className="w-44">
              <Select
                value={statusFilter}
                onChange={(e) =>
                  setStatusFilter(e.target.value as "" | PatientFormStatus)
                }
              >
                <option value="">All Status</option>
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
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
                    col.key === "appointmentDate" && "pl-6"
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
                            : "text-muted-foreground/50"
                        )}
                      />
                    )}
                  </span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((f) => (
              <TableRow
                key={f.id}
                className="cursor-pointer"
                onClick={() => openRecord(f)}
              >
                <TableCell className="pl-6">{f.appointmentDate}</TableCell>
                <TableCell className="text-muted-foreground">
                  {f.appointmentType}
                </TableCell>
                <TableCell>{f.chartNo}</TableCell>
                <TableCell className="font-medium">{f.patientName}</TableCell>
                <TableCell className="text-muted-foreground">
                  {f.memberPolicyId}
                </TableCell>
                <TableCell>{f.insuranceProvider}</TableCell>
                <TableCell>
                  <span
                    className={cn(
                      "inline-block rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide",
                      patientStatusStyles[f.status]
                    )}
                  >
                    {f.status}
                  </span>
                </TableCell>
              </TableRow>
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={COLUMNS.length}
                  className="py-10 text-center text-muted-foreground"
                >
                  No records match your filters.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>

      <RecordFormModal
        record={selected}
        open={modalOpen}
        onOpenChange={setModalOpen}
        onStatusChange={handleStatusChange}
      />
    </div>
  )
}
