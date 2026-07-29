import { useState, type FormEvent } from "react"
import { Link } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage } from "@/lib/api/client"
import { requestPasswordReset } from "@/lib/auth/api"

// Prefill the workspace for local dev convenience; the user can change it.
const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

// Pragmatic email shape check (mirrors Login.tsx) — the server is the source of truth.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function emailError(value: string): string | null {
  if (value.trim() === "") return "Email is required."
  if (!EMAIL_RE.test(value.trim())) return "Enter a valid email address."
  return null
}

export function ForgotPassword() {
  const [workspace, setWorkspace] = useState(DEFAULT_SLUG)
  const [email, setEmail] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [emailTouched, setEmailTouched] = useState(false)

  const emailErr = emailError(email)
  const showEmailErr = emailTouched && emailErr

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (emailErr) {
      setEmailTouched(true)
      return
    }
    setBusy(true)
    try {
      await requestPasswordReset(workspace.trim(), email.trim())
      // Same confirmation for any 2xx — the UI must not reveal whether the account exists.
      setSent(true)
    } catch (err) {
      setError(apiErrorMessage(err, "Something went wrong. Please try again."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Reset your password</CardTitle>
          <CardDescription>
            {sent
              ? "Check your email"
              : "Enter your workspace and email to receive a reset link."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {sent ? (
            <div className="space-y-4">
              <p className="text-sm text-muted-foreground">
                If an account exists for that email, we&apos;ve sent a password reset
                link. It expires in 1 hour.
              </p>
              <p className="text-center text-sm text-muted-foreground">
                <Link to="/login" className="underline underline-offset-4">Back to sign in</Link>
              </p>
            </div>
          ) : (
            <form onSubmit={onSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="workspace">Workspace</Label>
                <Input id="workspace" autoComplete="organization" required
                  placeholder="your-workspace"
                  value={workspace} onChange={(e) => setWorkspace(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="email">Email</Label>
                <Input id="email" type="email" autoComplete="username" required
                  aria-invalid={Boolean(showEmailErr)}
                  aria-describedby={showEmailErr ? "email-error" : undefined}
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  onBlur={() => setEmailTouched(true)} />
                {showEmailErr && (
                  <p id="email-error" className="text-sm text-destructive" role="alert">{emailErr}</p>
                )}
              </div>
              {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
              <Button type="submit" size="lg" className="w-full" disabled={busy}>
                {busy ? "Sending…" : "Send reset link"}
              </Button>
              <p className="text-center text-sm text-muted-foreground">
                <Link to="/login" className="underline underline-offset-4">Back to sign in</Link>
              </p>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
