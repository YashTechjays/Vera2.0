import { type JSX } from "react"
import { Loader2 } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { PromptVersionSummary } from "@/lib/api/prompts"

type VersionListProps = {
  versions: PromptVersionSummary[]
  loadedVersionId: string | null
  busy: boolean
  publishingId: string | null
  onLoad: (versionId: string) => void
  onPublish: (versionId: string) => void
}

/** Version history: every save is an immutable draft; one published per prompt.
 *  Load = the copy flow (edit + Save draft → new version). */
export function VersionList(props: VersionListProps): JSX.Element {
  if (props.versions.length === 0) {
    return <p className="text-sm text-muted-foreground">No versions yet.</p>
  }
  return (
    <div className="space-y-2">
      {props.versions.map((v) => (
        <div
          key={v.id}
          className={
            v.id === props.loadedVersionId
              ? "rounded-md border border-ring p-2"
              : "rounded-md border p-2"
          }
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">v{v.version}</span>
            <Badge variant={v.status === "published" ? "default" : "secondary"}>{v.status}</Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            pins schema v{v.schema_version} · {new Date(v.created_at).toLocaleDateString()}
          </p>
          <div className="mt-1.5 flex gap-1.5">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={props.busy}
              onClick={() => props.onLoad(v.id)}
            >
              Load
            </Button>
            {v.status !== "published" && (
              <Button
                type="button"
                size="sm"
                disabled={props.busy}
                onClick={() => props.onPublish(v.id)}
              >
                {props.publishingId === v.id ? <Loader2 className="animate-spin" /> : null} Publish
              </Button>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
