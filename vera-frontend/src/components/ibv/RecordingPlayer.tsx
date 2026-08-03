import { useEffect, useRef, useState } from "react"

import { getRecordingPlayback, type RecordingPlayback } from "@/lib/api/calls"
import { cachePlayback, evictPlayback, getFreshPlayback } from "@/lib/recordings/playbackCache"

/** Inline audio player for one call attempt's recording. Mounted only on the
 *  user's explicit click (CallHistoryTab), so the audited URL fetch is always
 *  user-initiated. On a mid-playback error past expiry it refetches once and
 *  resumes; a second failure surfaces the inline error. */
export function RecordingPlayer({ callId }: { callId: string }) {
  const [playback, setPlayback] = useState<RecordingPlayback | null>(() =>
    getFreshPlayback(callId),
  )
  const [failed, setFailed] = useState(false)
  const retried = useRef(false)
  const resumeAt = useRef(0)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    if (playback) return
    let cancelled = false
    getRecordingPlayback(callId)
      .then((p) => {
        cachePlayback(callId, p)
        if (!cancelled) setPlayback(p)
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetch once per mount
  }, [callId])

  if (failed)
    return (
      <p className="mt-2 text-xs text-destructive" role="alert">
        Recording unavailable.
      </p>
    )
  if (!playback) return <p className="mt-2 text-xs text-muted-foreground">Loading recording…</p>

  const handleError = () => {
    // Expired mid-listen (long pause, late seek): refetch once, resume position.
    if (!retried.current && getFreshPlayback(callId) === null) {
      retried.current = true
      resumeAt.current = audioRef.current?.currentTime ?? 0
      evictPlayback(callId)
      getRecordingPlayback(callId)
        .then((p) => {
          cachePlayback(callId, p)
          setPlayback(p)
        })
        .catch(() => setFailed(true))
      return
    }
    setFailed(true)
  }

  return (
    <audio
      ref={audioRef}
      className="mt-2 w-full"
      controls
      autoPlay
      preload="none"
      aria-label="Call recording"
      src={playback.url}
      onError={handleError}
      onLoadedMetadata={() => {
        if (resumeAt.current > 0 && audioRef.current) {
          audioRef.current.currentTime = resumeAt.current
          resumeAt.current = 0
        }
      }}
    />
  )
}
