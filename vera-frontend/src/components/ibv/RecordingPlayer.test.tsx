import { describe, expect, it, beforeEach } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { RecordingPlayer, cachePlayback, clearPlaybackCache } from "./RecordingPlayer"

const FRESH = { url: "https://storage.example/sig", expires_at: "2999-01-01T00:00:00Z" }
const EXPIRED = { url: "https://storage.example/old", expires_at: "2000-01-01T00:00:00Z" }

describe("RecordingPlayer", () => {
  beforeEach(() => clearPlaybackCache())

  it("renders the audio element from a cached fresh URL (no refetch needed)", () => {
    cachePlayback("c1", FRESH)
    const html = renderToStaticMarkup(<RecordingPlayer callId="c1" />)
    expect(html).toContain(`src="${FRESH.url}"`)
    expect(html).toContain('aria-label="Call recording"')
  })

  it("renders the loading state when no URL is cached", () => {
    const html = renderToStaticMarkup(<RecordingPlayer callId="c1" />)
    expect(html).toContain("Loading recording…")
    expect(html).not.toContain("<audio")
  })

  it("ignores an expired cached URL (never hands the audio element a dead link)", () => {
    cachePlayback("c1", EXPIRED)
    const html = renderToStaticMarkup(<RecordingPlayer callId="c1" />)
    expect(html).toContain("Loading recording…")
    expect(html).not.toContain(EXPIRED.url)
  })
})
