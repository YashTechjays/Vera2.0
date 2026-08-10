import { Button } from "@/components/ui/button"
import { lastPageOf } from "@/lib/pagination"

type PaginationFooterProps = {
  page: number
  pageSize: number
  total: number
  /** False until the first fetch resolves — shows "Loading…" instead of a zero count. */
  loaded: boolean
  /** Singular item word for the count label, e.g. "call". */
  noun: string
  onPageChange: (page: number) => void
}

export function PaginationFooter({
  page,
  pageSize,
  total,
  loaded,
  noun,
  onPageChange,
}: PaginationFooterProps) {
  const lastPage = lastPageOf(total, pageSize)
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-3">
      <span className="text-sm text-muted-foreground">
        {loaded
          ? `${total} ${noun}${total === 1 ? "" : "s"} · page ${page} of ${lastPage}`
          : "Loading…"}
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => onPageChange(Math.max(1, page - 1))}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={page >= lastPage}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </Button>
      </div>
    </div>
  )
}
