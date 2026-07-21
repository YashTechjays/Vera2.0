// Plain-text export of a live transcript, for the copy-transcript button.

import type { TranscriptTurn, TranscriptTurnSource } from "@/lib/api/callEvents"

/** Who to show for a turn: our bot is "Vera", a takeover shows the intervener. */
export function turnLabel(source: TranscriptTurnSource, supervisorLabel: string): string {
  if (source === "bot") return "Vera"
  if (source === "supervisor") return supervisorLabel
  return "Rep"
}

/** One line per turn; keypad presses read as an action, speech as "Label: text". */
export function transcriptText(turns: (TranscriptTurn & { supervisorLabel: string })[]): string {
  return turns
    .map((t) => {
      const label = turnLabel(t.source, t.supervisorLabel)
      return t.role === "dtmf" ? `${label} pressed ${t.text} on the keypad` : `${label}: ${t.text}`
    })
    .join("\n")
}
