import { beforeEach, describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { CallRow } from "./CallHistory"
import { cachePlayback, clearPlaybackCache } from "@/lib/recordings/playbackCache"
import type { CallHistoryRow } from "@/lib/api/calls"

const row = (over: Partial<CallHistoryRow> = {}): CallHistoryRow => ({
  id: "c1",
  form_id: "f1",
  mode: "full",
  status: "completed",
  created_at: "2026-07-21T12:00:00Z",
  patient_name: "Jane Doe",
  member_id: "POL-9",
  insurance_provider: "UHC",
  recording_available: false,
  transcript_available: false,
  ...over,
})

const noop = () => undefined

function render(c: CallHistoryRow, canPlay: boolean, playerOpen = false): string {
  return renderToStaticMarkup(
    <table>
      <tbody>
        <CallRow
          call={c}
          canPlay={canPlay}
          playerOpen={playerOpen}
          onOpenForm={noop}
          onTogglePlayer={noop}
        />
      </tbody>
    </table>,
  )
}

describe("CallRow", () => {
  beforeEach(() => clearPlaybackCache())

  it("renders the call's patient, policy, provider and status", () => {
    const html = render(row(), true)
    expect(html).toContain("Jane Doe")
    expect(html).toContain("POL-9")
    expect(html).toContain("UHC")
    expect(html).toContain("Completed")
  })

  it("shows the play control when the recording is available and the caller may play it", () => {
    expect(render(row({ recording_available: true }), true)).toContain("Play recording")
  })

  it("hides the play control when there is no playable recording", () => {
    expect(render(row({ recording_available: false }), true)).not.toContain("Play recording")
  })

  it("hides the play control without the recordings:read permission", () => {
    expect(render(row({ recording_available: true }), false)).not.toContain("Play recording")
  })

  it("renders the inline player (toggled label + audio) when open", () => {
    cachePlayback("c1", { url: "https://storage.example/sig", expires_at: "2999-01-01T00:00:00Z" })
    const html = render(row({ recording_available: true }), true, true)
    expect(html).toContain("Hide recording")
    expect(html).toContain('aria-label="Call recording"')
  })
})
