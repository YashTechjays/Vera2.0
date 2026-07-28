// The live list is poll-driven off the DB status, which lags a hangup by however
// long the worker's shutdown drain takes. The open call modal learns the real
// ending from SSE in ~2s, so the page pins that outcome onto the row until the
// server catches up (VR2-72). The pin carries the ACTUAL terminal status — a
// failed or canceled call must never read as a green "Completed".

import type { CallSummary } from "@/lib/api/calls"

export type EndedPin = {
  /** the real terminal status from SSE — never a hardcoded "completed" */
  status: string
  /** display-only, client-stamped: freezes the duration cell during the drain */
  ended_at: string
}

export type EndedPins = Map<string, EndedPin>

/** Record a call SSE reported terminal, keeping the first `ended_at` so the frozen
 *  duration can't drift when the effect re-fires with the same status. */
export function pinEnded(pins: EndedPins, callId: string, status: string, nowIso: string): EndedPin {
  const existing = pins.get(callId)
  const pin = existing?.status === status ? existing : { status, ended_at: existing?.ended_at ?? nowIso }
  pins.set(callId, pin)
  return pin
}

/** Apply the pins to a freshly polled list, dropping any the server has caught up
 *  on (a call it no longer lists as active). Mutates `pins`; returns new rows. */
export function applyEndedPins(pins: EndedPins, calls: CallSummary[]): CallSummary[] {
  for (const id of [...pins.keys()]) {
    if (!calls.some((c) => c.id === id)) pins.delete(id)
  }
  return calls.map((c) => {
    const pin = pins.get(c.id)
    return pin ? { ...c, status: pin.status, ended_at: pin.ended_at } : c
  })
}
