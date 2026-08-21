import { beforeEach, describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"

import { AttemptCard } from "./CallHistoryTab"
import { cachePlayback, clearPlaybackCache } from "@/lib/recordings/playbackCache"
import { parseSchema } from "@/lib/ibv/schema"
import type { FormSchema } from "@/lib/ibv/types"
import type { CallAttempt } from "@/lib/patient-forms/types"
// The backend's compiled artifact — same fixture src/lib/ibv/schema.test.ts uses — so a
// title-resolution test exercises a real leaf, not a hand-authored stand-in.
import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"

const schema = parseSchema(rawSchema)
// Title ("Copay ($)") differs from the humanized last path segment ("Copay") so a
// fallback-to-fieldLabel mutation is distinguishable from a real schema-title lookup —
// unlike a leaf whose title already equals its last segment (e.g. ".../total" → "Total").
const COPAY = "sections.infertility_treatment.ovulation_induction.copay" // leaf title: "Copay ($)"

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
  finalized: true,
  ...over,
})

const noop = () => undefined

function card(
  a: CallAttempt,
  canPlay: boolean,
  playerOpen = false,
  expanded = false,
  formSchema: FormSchema | null = null,
): string {
  return renderToStaticMarkup(
    <AttemptCard
      attempt={a}
      retriedAttempt={undefined}
      expanded={expanded}
      onToggleFields={noop}
      canPlay={canPlay}
      playerOpen={playerOpen}
      onTogglePlayer={noop}
      schema={formSchema}
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

describe("AttemptCard changed-field titles", () => {
  it("lists what an attempt changed by schema title, never a raw dotted path", () => {
    const html = card(attempt({ changed_paths: [COPAY] }), true, false, true, schema)
    // The exact schema-authored title, not just some substring a humanized fallback
    // ("Infertility Treatment › Ovulation Induction › Copay") would also satisfy.
    expect(html).toContain("Copay ($)")
    expect(html).not.toContain(COPAY)
  })

  it("falls back to a humanized path segment when the schema hasn't loaded", () => {
    const html = card(attempt({ changed_paths: [COPAY] }), true, false, true, null)
    // Proves this really is the no-schema fallback path, not a coincidentally-passing
    // schema lookup: the fallback never produces the schema's "($)" title suffix.
    expect(html).not.toContain("Copay ($)")
    expect(html).not.toContain(COPAY)
  })

  it("renders only the field's title and the overall count — never its collected value", () => {
    const html = card(attempt({ changed_paths: [COPAY] }), true, false, true, schema)
    expect(html).toContain("1 field updated")
    // "$25" is a plausible captured copay value (see test_call_authoritative.py's fixture) —
    // it must never appear in a view that names fields, not their values.
    expect(html).not.toContain("$25")
  })
})

describe("AttemptCard finalized marker", () => {
  it("says the outcome is unknown when the post-call eval never ran", () => {
    const html = card(attempt({ changed_paths: [], finalized: false }), true)
    expect(html).toMatch(/not finalized/i)
  })

  it("never claims an unfinalized attempt changed nothing", () => {
    const html = card(attempt({ changed_paths: [], finalized: false }), true)
    expect(html).not.toContain("0 fields updated")
  })

  it("says an attempt genuinely changed nothing once it is finalized", () => {
    const html = card(attempt({ changed_paths: [], finalized: true }), true)
    expect(html).toContain("0 fields updated")
    expect(html).not.toMatch(/not finalized/i)
  })

  it("treats a payload missing the flag as finalized (older backend contract)", () => {
    const legacy: Partial<CallAttempt> = attempt({ changed_paths: [] })
    delete legacy.finalized
    expect(card(legacy as CallAttempt, true)).not.toMatch(/not finalized/i)
  })

  it("shows no not-finalized marker for a call still in flight", () => {
    // after_state is {} for the whole life of a live call, not just once it fails —
    // the status badge already says "active"; the marker would be a false past-tense
    // claim ("was never finalized") about a call that hasn't finished yet.
    const html = card(attempt({ status: "active", changed_paths: [], finalized: false }), true)
    expect(html).not.toMatch(/not finalized/i)
    expect(html).toContain("0 fields updated")
  })
})
