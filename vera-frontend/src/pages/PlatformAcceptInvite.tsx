import { useState, type FormEvent, useEffect } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import {
  platformAcceptInvite, platformActivateInviteMfa, platformValidateInvite,
} from "@/lib/auth/api"

type Phase =
  | { kind: "checking" }
  | { kind: "invalid" }
  | { kind: "deactivated" }
  | { kind: "password" }
  | { kind: "mfa"; mfaToken: string; provisioningUri: string | null }
  | { kind: "done" }

export function PlatformAcceptInvite() {
  const [params] = useSearchParams()
  const token = params.get("token") ?? ""
  const navigate = useNavigate()

  const [phase, setPhase] = useState<Phase>(() => (token ? { kind: "checking" } : { kind: "invalid" }))
  const [password, setPassword] = useState("")
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const loginHref = "/platform/login"

  useEffect(() => {
    if (!token) return
    let cancelled = false
    platformValidateInvite(token)
      .then((res) => {
        if (cancelled) return
        if (res.state === "valid") {
          setPhase({ kind: "password" })
        } else if (res.state === "deactivated") {
          setPhase({ kind: "deactivated" })
        } else {
          setPhase({ kind: "invalid" })
        }
      })
      .catch(() => {
        if (!cancelled) setPhase({ kind: "invalid" })
      })
    return () => {
      cancelled = true
    }
  }, [token])

  async function onSetPassword(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await platformAcceptInvite(token, password)
      setPhase({ kind: "mfa", mfaToken: res.mfa_token ?? "", provisioningUri: res.provisioning_uri })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "This invitation is invalid or has expired.")
    } finally {
      setBusy(false)
    }
  }

  async function onActivateMfa(e: FormEvent) {
    e.preventDefault()
    if (phase.kind !== "mfa") return
    setError(null)
    setBusy(true)
    try {
      await platformActivateInviteMfa(phase.mfaToken, code)
      setPhase({ kind: "done" })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Activation failed.")
    } finally {
      setBusy(false)
    }
  }

  if (phase.kind === "checking") {
    return (
      <CenteredCard title="Checking invitation…" desc="Please wait a moment.">
        <div className="flex justify-center py-4">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </div>
      </CenteredCard>
    )
  }

  if (phase.kind === "invalid") {
    return (
      <CenteredCard title="Invalid invitation" desc="This invite link is missing, invalid, or has expired.">
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "deactivated") {
    return (
      <CenteredCard
        title="Account deactivated"
        desc="This account has been deactivated. Please contact another platform operator."
      >
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "done") {
    return (
      <CenteredCard title="Account active" desc="Your platform operator account is ready.">
        <Button className="w-full" onClick={() => navigate(loginHref, { replace: true })}>Sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "mfa") {
    return (
      <CenteredCard title="Set up two-factor" desc="Scan the QR code, then enter a code to finish. Two-factor authentication is required for all platform operators.">
        <div className="space-y-4">
          {phase.provisioningUri && (
            <div className="flex justify-center rounded-md bg-white p-4">
              <QRCodeSVG value={phase.provisioningUri} size={180} />
            </div>
          )}
          <form onSubmit={onActivateMfa} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="code">Authentication code</Label>
              <Input id="code" inputMode="numeric" autoComplete="one-time-code" required
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" className="w-full" disabled={busy}>{busy ? "Activating…" : "Activate"}</Button>
          </form>
        </div>
      </CenteredCard>
    )
  }

  return (
    <CenteredCard title="Accept your invitation" desc="Choose a password to activate your platform operator account.">
      <form onSubmit={onSetPassword} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="password">Password</Label>
          <PasswordInput id="password" autoComplete="new-password" required minLength={8}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Saving…" : "Set password"}</Button>
      </form>
    </CenteredCard>
  )
}

function CenteredCard({ title, desc, children }: { title: string; desc: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">{title}</CardTitle>
          <CardDescription>{desc}</CardDescription>
        </CardHeader>
        <CardContent>{children}</CardContent>
      </Card>
    </div>
  )
}
