import { useState, type FormEvent } from "react"
import { Link, Navigate, useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage, apiErrorStatus } from "@/lib/api/errors"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { platformLoginThunk, selectStatus } from "@/store/authSlice"

// Local-dev prefill (set in .env.local; empty in any real build). Mirrors the
// tenant login's VITE_DEV_* convenience for the platform operator credentials.
const DEV_EMAIL = import.meta.env.VITE_DEV_PLATFORM_EMAIL ?? ""
const DEV_PASSWORD = import.meta.env.VITE_DEV_PLATFORM_PASSWORD ?? ""

// Pragmatic email shape check; the server is the source of truth.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function emailError(value: string): string | null {
  if (value.trim() === "") return "Email is required."
  if (!EMAIL_RE.test(value.trim())) return "Enter a valid email address."
  return null
}

function passwordError(value: string): string | null {
  if (value === "") return "Password is required."
  return null
}

/** Platform-operator (super admin) sign-in. No workspace/slug — platform accounts
 *  belong to no tenant. MFA is always required, so a successful submit advances to
 *  the shared /mfa verification step (flagged platform). */
export function PlatformLogin() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const status = useAppSelector(selectStatus)

  const [email, setEmail] = useState(DEV_EMAIL)
  const [password, setPassword] = useState(DEV_PASSWORD)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [touched, setTouched] = useState({ email: false, password: false })

  if (status === "authenticated") return <Navigate to="/" replace />

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
      await dispatch(platformLoginThunk({ email, password })).unwrap()
      navigate("/mfa")
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
          <CardTitle className="text-lg">Platform operator sign in</CardTitle>
          <CardDescription>
            Super-admin access. You'll be asked for a two-factor code next.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                aria-invalid={Boolean(showEmailErr)}
                aria-describedby={showEmailErr ? "email-error" : undefined}
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, email: true }))}
              />
              {showEmailErr && (
                <p id="email-error" className="text-sm text-destructive" role="alert">
                  {emailErr}
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <PasswordInput
                id="password"
                autoComplete="current-password"
                required
                aria-invalid={Boolean(showPasswordErr)}
                aria-describedby={showPasswordErr ? "password-error" : undefined}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onBlur={() => setTouched((t) => ({ ...t, password: true }))}
              />
              {showPasswordErr && (
                <p id="password-error" className="text-sm text-destructive" role="alert">
                  {passwordErr}
                </p>
              )}
            </div>
            {error && (
              <p className="text-sm text-destructive" role="alert">
                {error}
              </p>
            )}
            <Button type="submit" size="lg" className="w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              <Link to="/login" className="underline underline-offset-4">
                Tenant sign in
              </Link>
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
