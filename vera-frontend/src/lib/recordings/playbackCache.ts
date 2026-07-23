import type { RecordingPlayback } from "@/lib/api/calls"

/** Signed playback URLs are valid ~10 min; cache per call so collapsing and
 *  re-expanding a player inside the TTL doesn't refetch — every fetch is a
 *  server-audited disclosure (RECORDING_ACCESSED). Module scope by design:
 *  the cache must survive the player component unmounting. */
const playbackCache = new Map<string, RecordingPlayback>()

function fresh(p: RecordingPlayback | undefined): p is RecordingPlayback {
  return !!p && new Date(p.expires_at).getTime() > Date.now()
}

export function cachePlayback(callId: string, playback: RecordingPlayback): void {
  playbackCache.set(callId, playback)
}

/** The cached URL for a call, or null if absent/expired (an expired URL must
 *  never reach the audio element — it would 400 on first byte). */
export function getFreshPlayback(callId: string): RecordingPlayback | null {
  const cached = playbackCache.get(callId)
  return fresh(cached) ? cached : null
}

export function evictPlayback(callId: string): void {
  playbackCache.delete(callId)
}

export function clearPlaybackCache(): void {
  playbackCache.clear()
}
