/** Last page number for a total — never below 1, so "page 1 of 1" renders even when empty. */
export function lastPageOf(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize))
}

/** The rows belonging to a 1-based page. */
export function slicePage<T>(rows: T[], page: number, pageSize: number): T[] {
  return rows.slice((page - 1) * pageSize, page * pageSize)
}
