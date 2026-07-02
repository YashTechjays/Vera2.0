import { useState } from "react"
import { Grid3x3 } from "lucide-react"
import { useConnectionState, useLocalParticipant } from "@livekit/components-react"
import { ConnectionState } from "livekit-client"

import { Button } from "@/components/ui/button"
import { Keypad } from "@/components/monitoring/Keypad"
import { sendDtmf } from "@/lib/voice-lab/dtmf"

/** Manual DTMF keypad trigger for an outbound Voice Lab call. Must render inside
 *  <LiveKitRoom> so the room hooks resolve. Publishes keypad tones from the browser's
 *  local participant; livekit-sip relays them to the phone leg. Failures bubble up via
 *  `onError` so the page surfaces them in its shared alert. */
export function VoiceLabDialpad({ onError }: { onError?: (message: string) => void }) {
  const { localParticipant } = useLocalParticipant()
  const state = useConnectionState()
  const [open, setOpen] = useState(false)

  const connected = state === ConnectionState.Connected

  async function handleSend(digits: string) {
    // Count only in every log — a pressed digit sequence can be PHI (e.g. a member ID),
    // so it never lands in the browser console (mirrors backend agent_worker/dtmf.py).
    if (!connected) {
      console.warn("[DTMF] ✗ not connected — ignoring %d digit(s)", digits.length)
      onError?.("Not connected to the call yet.")
      return
    }
    console.info("[DTMF] sending %d digit(s)", digits.length)
    try {
      await sendDtmf(localParticipant, digits)
      console.info("[DTMF] ✓ published %d digit(s) to LiveKit", digits.length)
    } catch (err) {
      console.error("[DTMF] ✗ failed:", err)
      onError?.("Could not send those keys.")
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
        disabled={!connected}
        title={connected ? "Open the keypad" : "Connect to the call first"}
      >
        <Grid3x3 /> Keypad
      </Button>
      <Keypad open={open} onOpenChange={setOpen} onSend={handleSend} />
    </>
  )
}
