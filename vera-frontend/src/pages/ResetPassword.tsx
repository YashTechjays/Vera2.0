import { useState, type FormEvent, useEffect } from "react"
import { useNavigate, useParams, useSearchParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { confirmPasswordReset, validatePasswordReset } from "@/lib/auth/api"

type Phase =
  | { kind: "checking" }
  | { kind: "invalid" }
  | { kind: "deactivated" }
  | { kind: "password" }
  | { kind: "done" }

export function ResetPassword() {
  const { tenantSlug = "" } = useParams()
  const [params] = useSearchParams()
  const token = params.get("token") ?? ""
  const navigate = useNavigate()

  const [phase, setPhase] = useState<Phase>(() =>
    token ? { kind: "checking" } : { kind: "invalid" },
  )
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const loginHref = "/login"

  useEffect(() => {
    if (!token) return
    let cancelled = false
    validatePasswordReset(tenantSlug, token)
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
  }, [tenantSlug, token])

  async function onSetPassword(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await confirmPasswordReset(tenantSlug, token, password)
      setPhase({ kind: "done" })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "This reset link is invalid or has expired.")
    } finally {
      setBusy(false)
    }
  }

  if (phase.kind === "checking") {
    return (
      <CenteredCard title="Checking reset link…" desc="Please wait a moment.">
        <div className="flex justify-center py-4">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        </div>
      </CenteredCard>
    )
  }

  if (phase.kind === "invalid") {
    return (
      <CenteredCard
        title="Invalid reset link"
        desc="This link is missing, invalid, or has expired. You can request a new one from the sign-in page."
      >
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "deactivated") {
    return (
      <CenteredCard
        title="Account deactivated"
        desc="This account has been deactivated. Please contact your administrator."
      >
        <Button className="w-full" onClick={() => navigate(loginHref)}>Go to sign in</Button>
      </CenteredCard>
    )
  }

  if (phase.kind === "done") {
    return (
      <CenteredCard title="Password updated" desc="Sign in with your new password.">
        <Button className="w-full" onClick={() => navigate(loginHref, { replace: true })}>Sign in</Button>
      </CenteredCard>
    )
  }

  return (
    <CenteredCard title="Set a new password" desc="Choose a new password for your account.">
      <form onSubmit={onSetPassword} className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="password">New password</Label>
          <PasswordInput id="password" autoComplete="new-password" required minLength={8}
            value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" className="w-full" disabled={busy}>{busy ? "Saving…" : "Set new password"}</Button>
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
