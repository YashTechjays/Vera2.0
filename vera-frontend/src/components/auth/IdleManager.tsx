import { useCallback, useEffect, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import { IdleWarningDialog } from "@/components/auth/IdleWarningDialog"
import {
  computeIdleState,
  KEEPALIVE_THROTTLE_MS,
  LIVE_CALL_ACTIVITY_EVENT,
} from "@/lib/auth/idle"
import {
  keepaliveThunk,
  logoutThunk,
  selectIdleTimeoutMs,
  selectSessionExpiresAt,
} from "@/store/authSlice"
import { useAppDispatch, useAppSelector } from "@/store/hooks"

// User input plus the live-call beacon: an open live-call connection counts as
// activity (the supervisor is actively listening even without touching the page).
const ACTIVITY_EVENTS = [
  "mousemove",
  "mousedown",
  "keydown",
  "scroll",
  "touchstart",
  LIVE_CALL_ACTIVITY_EVENT,
] as const

// Single idle-manager. Mounted only inside AppShell (authenticated), so every
// listener/timer is torn down on logout via the effect cleanups when it unmounts.
export function IdleManager() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  // Timeout config is backend-driven (from /me), never hardcoded here.
  const idleTimeoutMs = useAppSelector(selectIdleTimeoutMs)
  const sessionExpiresAt = useAppSelector(selectSessionExpiresAt)

  const lastActivity = useRef(0)
  const lastKeepalive = useRef(0)
  const warningRef = useRef(false) // mirror of `warning` for event handlers
  const loggingOut = useRef(false)

  const [warning, setWarning] = useState(false)
  const [secondsLeft, setSecondsLeft] = useState(0)

  const doLogout = useCallback(() => {
    if (loggingOut.current) return
    loggingOut.current = true
    void dispatch(logoutThunk()).finally(() => {
      navigate("/login", { replace: true })
    })
  }, [dispatch, navigate])

  const staySignedIn = useCallback(() => {
    const now = Date.now()
    lastActivity.current = now
    lastKeepalive.current = now
    warningRef.current = false
    setWarning(false)
    void dispatch(keepaliveThunk())
  }, [dispatch])

  // Activity → reset idle timer + throttled keepalive. Ignored while the warning
  // shows, so only the explicit "Stay signed in" can rescue the session.
  useEffect(() => {
    function onActivity() {
      if (warningRef.current) return
      const now = Date.now()
      lastActivity.current = now
      if (now - lastKeepalive.current >= KEEPALIVE_THROTTLE_MS) {
        lastKeepalive.current = now
        void dispatch(keepaliveThunk())
      }
    }
    ACTIVITY_EVENTS.forEach((e) => window.addEventListener(e, onActivity, { passive: true }))
    return () => ACTIVITY_EVENTS.forEach((e) => window.removeEventListener(e, onActivity))
  }, [dispatch])

  // 1s tick using real timestamps + immediate recompute on focus/visibility so a
  // backgrounded or slept tab logs out correctly on return.
  useEffect(() => {
    const now = Date.now()
    if (lastActivity.current === 0) lastActivity.current = now
    if (lastKeepalive.current === 0) lastKeepalive.current = now

    function check() {
      // /me hasn't hydrated the timeout config yet — wait for it rather than
      // logging out a freshly-authenticated session on a transient null.
      if (idleTimeoutMs === null || sessionExpiresAt === null) return
      const state = computeIdleState({
        now: Date.now(),
        lastActivity: lastActivity.current,
        idleTimeoutMs,
        absoluteDeadline: sessionExpiresAt,
      })
      if (state.phase === "expired") {
        doLogout()
        return
      }
      const show = state.phase === "warning"
      warningRef.current = show
      setWarning(show)
      if (show) setSecondsLeft(state.secondsLeft)
    }
    const id = window.setInterval(check, 1000)
    document.addEventListener("visibilitychange", check)
    window.addEventListener("focus", check)
    check()
    return () => {
      window.clearInterval(id)
      document.removeEventListener("visibilitychange", check)
      window.removeEventListener("focus", check)
    }
  }, [doLogout, idleTimeoutMs, sessionExpiresAt])

  return (
    <IdleWarningDialog
      open={warning}
      secondsLeft={secondsLeft}
      onStay={staySignedIn}
      onLogout={doLogout}
    />
  )
}
