import { useCallback, useEffect, useRef, useState } from "react"

/** Copy text to the clipboard; false on denial or insecure context. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
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
