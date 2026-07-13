// The inverse of the backend intake flattener (`iter_leaf_answers`): the
// renderer's flat values map (root-anchored `sections.<key>...` paths) becomes
// the nested-by-section intake_payload POST /patient-forms:create expects.
// Empty values are omitted — the backend treats blank as "not provided".

import type { FormValues } from "@/lib/ibv/types"

const SECTIONS_PREFIX = "sections."

export function valuesToIntakePayload(values: FormValues): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  for (const [path, raw] of Object.entries(values)) {
    const value = (raw ?? "").trim()
    if (value === "" || !path.startsWith(SECTIONS_PREFIX)) continue
    const parts = path.slice(SECTIONS_PREFIX.length).split(".")
    let node = payload
    for (const part of parts.slice(0, -1)) {
      const existing = node[part]
      if (typeof existing === "object" && existing !== null) {
        node = existing as Record<string, unknown>
      } else {
        const child: Record<string, unknown> = {}
        node[part] = child
        node = child
      }
    }
    node[parts[parts.length - 1]] = value
  }
  return payload
}
