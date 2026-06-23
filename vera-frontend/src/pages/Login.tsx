import { useState, type FormEvent } from "react"
import { Navigate, useNavigate, useLocation } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { PasswordInput } from "@/components/ui/password-input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { loginThunk, selectStatus } from "@/store/authSlice"

const DEV_EMAIL = import.meta.env.VITE_DEV_EMAIL ?? ""
const DEV_PASSWORD = import.meta.env.VITE_DEV_PASSWORD ?? ""
// Prefill the workspace for local dev convenience; the user can change it.
const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

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

  if (status === "authenticated") return <Navigate to={from} replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
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
      setError(err instanceof ApiError && err.httpStatus === 401
        ? "Invalid credentials."
        : err instanceof ApiError ? err.message : "Something went wrong.")
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
                value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <PasswordInput id="password" autoComplete="current-password" required
                value={password} onChange={(e) => setPassword(e.target.value)} />
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
