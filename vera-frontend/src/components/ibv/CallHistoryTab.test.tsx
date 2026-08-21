import { beforeEach, describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { AttemptCard } from "./CallHistoryTab"
import { cachePlayback, clearPlaybackCache } from "@/lib/recordings/playbackCache"
import type { CallAttempt } from "@/lib/patient-forms/types"

const attempt = (over: Partial<CallAttempt>): CallAttempt => ({
  id: "c1",
  attempt: 1,
  mode: "full",
  status: "completed",
  created_at: "2026-07-21T12:00:00Z",
  retry_of: null,
  changed_paths: [],
  recording_available: false,
  authoritative: true,
  ...over,
})

const noop = () => undefined

function card(a: CallAttempt, canPlay: boolean, playerOpen = false): string {
  return renderToStaticMarkup(
    <AttemptCard
      attempt={a}
      retriedAttempt={undefined}
      expanded={false}
      onToggleFields={noop}
      canPlay={canPlay}
      playerOpen={playerOpen}
      onTogglePlayer={noop}
    />,
  )
}

describe("AttemptCard recording playback", () => {
  beforeEach(() => clearPlaybackCache())

  it("shows the play control when the recording is available and the caller may play it", () => {
    expect(card(attempt({ recording_available: true }), true)).toContain("Play recording")
  })

  it("hides the play control when the attempt has no playable recording", () => {
    expect(card(attempt({ recording_available: false }), true)).not.toContain("Play recording")
  })

  it("hides the play control without the recordings:read permission", () => {
    expect(card(attempt({ recording_available: true }), false)).not.toContain("Play recording")
  })

  it("renders the player (toggled label + audio) when open", () => {
    cachePlayback("c1", { url: "https://storage.example/sig", expires_at: "2999-01-01T00:00:00Z" })
    const html = card(attempt({ recording_available: true }), true, true)
    expect(html).toContain("Hide recording")
    expect(html).toContain('aria-label="Call recording"')
  })
})

describe("AttemptCard authoritative marker", () => {
  it("marks an attempt that captured no reference number", () => {
    expect(card(attempt({ authoritative: false }), true)).toContain("No call reference")
  })

  it("does not mark an authoritative attempt", () => {
    expect(card(attempt({ authoritative: true }), true)).not.toContain("No call reference")
  })

  it("treats a payload missing the flag as authoritative (older backend contract)", () => {
    const legacy: Partial<CallAttempt> = attempt({ authoritative: false })
    delete legacy.authoritative
    expect(card(legacy as CallAttempt, true)).not.toContain("No call reference")
  })
})
