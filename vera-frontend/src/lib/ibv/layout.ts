/** Width cap shared by capValue rows and the rail aside — one constant so the
 *  control and the column it sits in can never drift apart (VR2-162). */
export const VALUE_CAP_CLASS = "w-[420px] shrink-0"

/**
 * Greedily place each item into the currently shorter column, preserving the items'
 * relative order within each column. Used for the form's section runs (VR2-162):
 * strict left/right alternation left a tall section beside a one-row section with a
 * huge blank hole.
 */
export function packTwoColumns<T>(
  items: T[],
  heightOf: (item: T) => number
): [T[], T[]] {
  const columns: [T[], T[]] = [[], []]
  const heights: [number, number] = [0, 0]
  for (const item of items) {
    const shorter = heights[0] <= heights[1] ? 0 : 1
    columns[shorter].push(item)
    heights[shorter] += heightOf(item)
  }
  return columns
}
