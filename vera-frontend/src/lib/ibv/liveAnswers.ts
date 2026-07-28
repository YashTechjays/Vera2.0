/** Whether an AI answer pushed over SSE may be written into the currently loaded form. */
export function canApplyLiveAnswer({
  loadedFormId,
  expectedFormId,
  path,
  editedPaths,
}: {
  loadedFormId: string | null
  expectedFormId: string
  path: string
  editedPaths: ReadonlySet<string>
}): boolean {
  // A stream outlives closeForm(), so the id match is what stops a stale one writing into
  // whatever form is loaded now; an edited path means the supervisor's value outranks the agent.
  return loadedFormId === expectedFormId && !editedPaths.has(path)
}
