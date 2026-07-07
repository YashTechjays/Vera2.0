import { useState, type FormEvent } from "react"
import { Navigate, useNavigate, useLocation } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage, apiErrorStatus } from "@/lib/api/errors"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { loginThunk, selectStatus } from "@/store/authSlice"

const DEV_EMAIL = import.meta.env.VITE_DEV_EMAIL ?? ""
const DEV_PASSWORD = import.meta.env.VITE_DEV_PASSWORD ?? ""
// Prefill the workspace for local dev convenience; the user can change it.
const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

// Pragmatic email shape check (non-empty local + domain with a dot) — the server
// is the source of truth; this just catches obvious typos before a round-trip.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const MIN_PASSWORD_LENGTH = 8

function emailError(value: string): string | null {
  if (value.trim() === "") return "Email is required."
  if (!EMAIL_RE.test(value.trim())) return "Enter a valid email address."
  return null
}

function passwordError(value: string): string | null {
  if (value === "") return "Password is required."
  if (value.length < MIN_PASSWORD_LENGTH)
    return `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`
  return null
}

export function Login() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const location = useLocation()
  const status = useAppSelector(selectStatus)
  const from = (location.state as { from?: string } | null)?.from ?? "/"

  const [workspace, setWorkspace] = useState(DEFAULT_SLUG)
  const [email, setEmail] = useState(DEV_EMAIL)
  const [password, setPassword] = useState(DEV_PASSWORD)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Field errors surface once a field is touched (blurred) or on submit attempt.
  const [touched, setTouched] = useState({ email: false, password: false })

  if (status === "authenticated") return <Navigate to={from} replace />

  const emailErr = emailError(email)
  const passwordErr = passwordError(password)
  const showEmailErr = touched.email && emailErr
  const showPasswordErr = touched.password && passwordErr

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (emailErr || passwordErr) {
      setTouched({ email: true, password: true })
      return
    }
    setBusy(true)
    try {
      const res = await dispatch(
        loginThunk({ slug: workspace.trim(), email, password }),
      ).unwrap()
      if (res === "none") {
        navigate(from, { replace: true })
      } else {
        navigate(res === "verify" ? "/mfa" : "/mfa-enroll")
      }
    } catch (err) {
      setError(
        apiErrorStatus(err) === 401
          ? "Invalid credentials."
          : apiErrorMessage(err, "Something went wrong."),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Sign in to Vera</CardTitle>
          <CardDescription>Enter your workspace and credentials.</CardDescription>
        </CardHeader>
        <CardContent>
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
                onBlur={() => setTouched((t) => ({ ...t, email: true }))} />
              {showEmailErr && (
                <p id="email-error" className="text-sm text-destructive" role="alert">{emailErr}</p>
              )}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <PasswordInput id="password" autoComplete="current-password" required
                aria-invalid={Boolean(showPasswordErr)}
                aria-describedby={showPasswordErr ? "password-error" : undefined}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, password: true }))} />
              {showPasswordErr && (
                <p id="password-error" className="text-sm text-destructive" role="alert">{passwordErr}</p>
              )}
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" size="lg" className="w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
