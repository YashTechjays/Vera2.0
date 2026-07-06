import type { Condition, FormValues } from "./types"

/**
 * Evaluate a DSL v2 condition against the current form values (spec §4.5).
 * Field references are root-anchored paths; a missing value evaluates as ""
 * (so `eq` is false and `ne`/`not_in` are true until the field is answered).
 * An unknown `ref` evaluates to false — never throw or log (values are PHI).
 */
export function evaluateCondition(
  cond: Condition,
  values: FormValues,
  shared: Record<string, Condition> = {}
): boolean {
  if ("ref" in cond) {
    const target = shared[cond.ref]
    return target ? evaluateCondition(target, values, shared) : false
  }
  if ("all" in cond) return cond.all.every((c) => evaluateCondition(c, values, shared))
  if ("any" in cond) return cond.any.some((c) => evaluateCondition(c, values, shared))
  if ("not" in cond) return !evaluateCondition(cond.not, values, shared)

  const v = values[cond.field] ?? ""
  switch (cond.op) {
    case "eq":
      return v === cond.value
    case "ne":
      return v !== cond.value
    case "in":
      return (cond.value as string[]).includes(v)
    case "not_in":
      return !(cond.value as string[]).includes(v)
  }
}
