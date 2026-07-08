import { useCallback, useEffect, useRef, useState } from "react"

/** Copy text to the clipboard, working in both secure and non-secure contexts.
 *
 * `navigator.clipboard` only exists in secure contexts (HTTPS or localhost).
 * Our dev deployments serve plain HTTP, so we fall back to the legacy
 * textarea + `document.execCommand("copy")` path there. Returns whether the
 * copy actually succeeded so callers can show accurate feedback.
 */
export async function copyText(text: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission denied or transient failure — try the legacy path.
    }
  }

  const textarea = document.createElement("textarea")
  textarea.value = text
  textarea.setAttribute("readonly", "")
  // Keep it out of view without triggering scroll-to-focus jumps.
  textarea.style.position = "fixed"
  textarea.style.opacity = "0"
  document.body.appendChild(textarea)
  textarea.select()
  try {
    return document.execCommand("copy")
  } catch {
    return false
  } finally {
    textarea.remove()
  }
}

export type CopyState = "idle" | "copied" | "failed"

/** Copy with visible feedback: flips to copied/failed, back to idle after resetMs. */
export function useCopy(resetMs = 2000): {
  state: CopyState
  copy: (text: string) => Promise<void>
} {
  const [state, setState] = useState<CopyState>("idle")
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const copy = useCallback(
    async (text: string) => {
      setState((await copyText(text)) ? "copied" : "failed")
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => setState("idle"), resetMs)
    },
    [resetMs],
  )
  return { state, copy }
}
