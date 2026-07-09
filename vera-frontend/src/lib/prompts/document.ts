// Pure editing/parsing helpers for the /agent-prompt editor. No React, no I/O —
// the unit-tested core (spec 2026-07-09 §3.1). PromptDocument semantics:
// `session` is literal content; `task_overrides` is a sparse patch where an
// absent field falls through to the schema default and an EMPTY override is
// invalid server-side (min_length=1) — removal, never blanking.
import type { PromptDocument, TaskTextOverride } from "@/lib/api/prompts"

export type OverrideField = "intro" | "outro" | "prompt"

export type OverrideState = "overridden" | "default" | "no-default"

/** One schema task's authored text defaults (from the raw schema document). */
export type TaskDefaults = {
  task_key: string
  title: string
  intro?: string
  outro?: string
  prompt?: string
}

export type PlaceholderEntry = { token: string; detail: string }

export type PlaceholderGroups = {
  /** system_fields keys; detail = the mapped field path */
  system: PlaceholderEntry[]
  /** role:"context" leaf paths; detail = the field title */
  context: PlaceholderEntry[]
}

export type ParsedErrors = {
  /** location (e.g. "task_overrides.wrap_up.outro") → messages */
  fields: Record<string, string[]>
  /** messages with no recognizable field location */
  general: string[]
}

const OVERRIDE_FIELDS: OverrideField[] = ["intro", "outro", "prompt"]

function presentFields(override: TaskTextOverride): TaskTextOverride {
  const out: TaskTextOverride = {}
  for (const field of OVERRIDE_FIELDS) {
    const value = override[field]
    if (typeof value === "string") out[field] = value
  }
  return out
}

/** Strip the server's explicit nulls and any empty entries so the editing
 *  buffer only ever carries present string fields. */
export function normalizeDocument(doc: PromptDocument): PromptDocument {
  const task_overrides: Record<string, TaskTextOverride> = {}
  for (const [key, override] of Object.entries(doc.task_overrides)) {
    const present = presentFields(override)
    if (Object.keys(present).length > 0) task_overrides[key] = present
  }
  return { kind: "prompt_document", session: { ...doc.session }, task_overrides }
}

export function setOverrideField(
  doc: PromptDocument,
  taskKey: string,
  field: OverrideField,
  text: string,
): PromptDocument {
  const entry = { ...presentFields(doc.task_overrides[taskKey] ?? {}), [field]: text }
  return { ...doc, task_overrides: { ...doc.task_overrides, [taskKey]: entry } }
}

/** "Reset to default": remove the override field; drop the entry when empty. */
export function clearOverrideField(
  doc: PromptDocument,
  taskKey: string,
  field: OverrideField,
): PromptDocument {
  const entry = presentFields(doc.task_overrides[taskKey] ?? {})
  delete entry[field]
  if (Object.keys(entry).length === 0) return removeOverrideEntry(doc, taskKey)
  return { ...doc, task_overrides: { ...doc.task_overrides, [taskKey]: entry } }
}

export function removeOverrideEntry(doc: PromptDocument, taskKey: string): PromptDocument {
  const task_overrides = { ...doc.task_overrides }
  delete task_overrides[taskKey]
  return { ...doc, task_overrides }
}

export function overrideStateOf(
  doc: PromptDocument,
  taskKey: string,
  field: OverrideField,
  defaultText: string | undefined,
): OverrideState {
  if (typeof doc.task_overrides[taskKey]?.[field] === "string") return "overridden"
  if (defaultText === undefined) return "no-default"
  return "default"
}

function canonical(doc: PromptDocument): string {
  const normalized = normalizeDocument(doc)
  const keys = Object.keys(normalized.task_overrides).sort()
  return JSON.stringify({
    session: [
      normalized.session.persona,
      normalized.session.goal,
      normalized.session.base_instructions,
    ],
    overrides: keys.map((key) => {
      const entry = normalized.task_overrides[key]
      return [key, entry.intro ?? null, entry.outro ?? null, entry.prompt ?? null]
    }),
  })
}

/** Dirty check: null-vs-absent and key order do not count as changes. */
export function documentsEqual(a: PromptDocument, b: PromptDocument): boolean {
  return canonical(a) === canonical(b)
}

/** Pre-save checks the server would reject with 422/400 shape errors.
 *  Placeholder validation is deliberately NOT done client-side — the preview
 *  endpoint's `errors` is the authority (spec §3.6). */
export function clientValidationErrors(doc: PromptDocument): Record<string, string[]> {
  const errors: Record<string, string[]> = {}
  const session: Record<string, string> = {
    persona: doc.session.persona,
    goal: doc.session.goal,
    base_instructions: doc.session.base_instructions,
  }
  for (const [field, value] of Object.entries(session)) {
    if (value.trim() === "") errors[`session.${field}`] = ["Required."]
  }
  for (const [key, override] of Object.entries(doc.task_overrides)) {
    for (const field of OVERRIDE_FIELDS) {
      const value = override[field]
      if (typeof value === "string" && value.trim() === "") {
        errors[`task_overrides.${key}.${field}`] = [
          "An override cannot be empty — use Reset to remove it.",
        ]
      }
    }
  }
  return errors
}

const FIELD_LOCATION_RE =
  /^(session\.(?:persona|goal|base_instructions)|task_overrides\.[^\s.:]+\.(?:intro|outro|prompt)): (.*)$/

/** Split the server's "; "-joined, location-prefixed content errors (draft-save
 *  400 message and POST-preview `errors[]` use identical strings). */
export function parsePromptErrors(joined: string): ParsedErrors {
  const fields: Record<string, string[]> = {}
  const general: string[] = []
  for (const part of joined.split("; ")) {
    const message = part.trim()
    if (message === "") continue
    const match = FIELD_LOCATION_RE.exec(message)
    if (match === null) {
      general.push(message)
    } else {
      const existing = fields[match[1]] ?? []
      fields[match[1]] = [...existing, match[2]]
    }
  }
  return { fields, general }
}

// ---------------------------------------------------------------------------
// Raw schema document extraction. The ibv/types.ts UI subset intentionally
// omits `tasks`, so this module reads the raw JSON with its own narrow types.
// ---------------------------------------------------------------------------

type RawRecord = Record<string, unknown>

function asRecord(value: unknown): RawRecord | null {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as RawRecord
  }
  return null
}

function asOptionalText(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined
}

export function taskDefaultsOf(rawDoc: unknown): TaskDefaults[] {
  const tasks = asRecord(rawDoc)?.tasks
  if (!Array.isArray(tasks)) return []
  const out: TaskDefaults[] = []
  for (const raw of tasks) {
    const task = asRecord(raw)
    if (task === null) continue
    const taskKey = asOptionalText(task.task_key)
    if (taskKey === undefined) continue
    out.push({
      task_key: taskKey,
      title: asOptionalText(task.title) ?? taskKey,
      intro: asOptionalText(task.intro),
      outro: asOptionalText(task.outro),
      prompt: asOptionalText(task.prompt),
    })
  }
  return out
}

function collectContextLeaves(prefix: string, fields: unknown, out: PlaceholderEntry[]): void {
  const record = asRecord(fields)
  if (record === null) return
  for (const [key, raw] of Object.entries(record)) {
    const field = asRecord(raw)
    if (field === null) continue
    const path = `${prefix}.${key}`
    if (field.type === "group") {
      collectContextLeaves(path, field.fields, out)
    } else if (field.role === "context") {
      out.push({ token: path, detail: asOptionalText(field.title) ?? path })
    }
  }
}

/** The valid {{token}} namespace of a schema document: system_fields keys plus
 *  root-anchored paths of role:"context" leaves (spec 2026-07-08 §4). */
export function placeholderGroupsOf(rawDoc: unknown): PlaceholderGroups {
  const doc = asRecord(rawDoc)
  const system: PlaceholderEntry[] = []
  const context: PlaceholderEntry[] = []
  const systemFields = asRecord(doc?.system_fields)
  if (systemFields !== null) {
    for (const [token, path] of Object.entries(systemFields)) {
      system.push({ token, detail: asOptionalText(path) ?? "" })
    }
  }
  const sections = asRecord(doc?.sections)
  if (sections !== null) {
    for (const [sectionKey, rawSection] of Object.entries(sections)) {
      const section = asRecord(rawSection)
      if (section !== null) {
        collectContextLeaves(`sections.${sectionKey}`, section.fields, context)
      }
    }
  }
  return { system, context }
}

/** Insert `{{token}}` at the caret (append when unknown); returns the new caret. */
export function insertToken(
  text: string,
  token: string,
  caret: number | null,
): { next: string; caret: number } {
  const at = caret ?? text.length
  const inserted = `{{${token}}}`
  return { next: `${text.slice(0, at)}${inserted}${text.slice(at)}`, caret: at + inserted.length }
}
