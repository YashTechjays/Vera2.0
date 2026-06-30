import type { PromptVersionSummary } from "@/lib/api/prompts"

/** The version to load into the editor by default: the published one, else the
 *  newest (versions arrive newest-first), else none. */
export function pickInitialVersion(
  versions: PromptVersionSummary[],
): PromptVersionSummary | undefined {
  return versions.find((v) => v.status === "published") ?? versions[0]
}
