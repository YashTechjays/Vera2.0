import { useState, type FormEvent } from "react"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { RecoveryCodes } from "@/components/auth/RecoveryCodes"
import { ApiError } from "@/lib/api/client"
import { activateMfa, enrollMfa } from "@/lib/auth/api"

type Phase =
  | { kind: "idle" }
  | { kind: "enroll"; provisioningUri: string }
  | { kind: "recovery"; codes: string[] }

// Self-service MFA setup for the signed-in user, using the authenticated
// /auth/mfa/enroll + /auth/mfa/activate endpoints. (The API does not expose
// current enabled/disabled status, so this is a setup action, not a status row.)
export function MfaSetupDialog() {
  const [open, setOpen] = useState(false)
  const [phase, setPhase] = useState<Phase>({ kind: "idle" })
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function reset() {
    setOpen(false)
    setPhase({ kind: "idle" })
    setCode("")
    setError(null)
    setBusy(false)
  }

  async function startEnroll() {
    setError(null)
    setBusy(true)
    try {
      const res = await enrollMfa()
      setPhase({ kind: "enroll", provisioningUri: res.provisioning_uri })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start enrollment.")
    } finally {
      setBusy(false)
    }
  }

  async function onActivate(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await activateMfa(code)
      setPhase({ kind: "recovery", codes: res.recovery_codes })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Invalid code. Try again.")
    } finally {
      setBusy(false)
    }
  }

  return (
    // Dismissal is blocked while busy: closing mid-activate resets the UI
    // while MFA silently activates server-side.
    <Dialog open={open} onOpenChange={(o) => (o ? setOpen(true) : busy ? undefined : reset())}>
      <DialogTrigger asChild>
        <Button variant="outline">Set up two-factor authentication</Button>
      </DialogTrigger>
      <DialogContent showCloseButton className="max-w-md gap-0 p-0">
        <DialogHeader className="border-b border-border p-5 pr-12">
          <DialogTitle className="text-base font-semibold">Two-factor authentication</DialogTitle>
          <DialogDescription>
            {phase.kind === "recovery"
              ? "Save your recovery codes to finish."
              : "Protect your account with a time-based one-time code."}
          </DialogDescription>
        </DialogHeader>

        {phase.kind === "idle" && (
          <>
            <div className="space-y-3 p-5 text-sm text-muted-foreground">
              <p>
                You'll scan a QR code with an authenticator app (Google Authenticator,
                1Password, Authy…), confirm a 6-digit code, then save a set of recovery codes.
              </p>
              {error && <p className="text-destructive" role="alert">{error}</p>}
            </div>
            <div className="flex justify-end gap-3 border-t border-border p-4">
              <Button type="button" variant="outline" onClick={reset}>Cancel</Button>
              <Button onClick={startEnroll} disabled={busy} className="min-w-[120px]">
                {busy ? "Starting…" : "Begin setup"}
              </Button>
            </div>
          </>
        )}

        {phase.kind === "enroll" && (
          <form onSubmit={onActivate}>
            <div className="space-y-4 p-5">
              <div className="flex justify-center rounded-md bg-white p-4">
                <QRCodeSVG value={phase.provisioningUri} size={180} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="mfa-setup-code">Authentication code</Label>
                <Input
                  id="mfa-setup-code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  required
                  autoFocus
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            </div>
            <div className="flex justify-end gap-3 border-t border-border p-4">
              <Button type="button" variant="outline" onClick={reset}>Cancel</Button>
              <Button type="submit" disabled={busy} className="min-w-[120px]">
                {busy ? "Activating…" : "Activate"}
              </Button>
            </div>
          </form>
        )}

        {phase.kind === "recovery" && (
          <div className="p-5">
            <RecoveryCodes codes={phase.codes} onContinue={reset} />
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
