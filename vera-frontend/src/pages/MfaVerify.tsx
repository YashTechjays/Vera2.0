import { useState, type FormEvent } from "react"
import { Navigate, useNavigate, useParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api/client"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { selectMfa, verifyMfaThunk } from "@/store/authSlice"

export function MfaVerify() {
  const { tenantSlug = "" } = useParams()
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const mfa = useAppSelector(selectMfa)
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // No challenge in state (e.g. refresh) → back to login.
  if (!mfa || mfa.step !== "verify") return <Navigate to={`/tenants/${tenantSlug}/login`} replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await dispatch(verifyMfaThunk({ slug: tenantSlug, mfaToken: mfa!.token, code })).unwrap()
      navigate("/", { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Verification failed.")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Two-factor verification</CardTitle>
          <CardDescription>Enter the 6-digit code from your authenticator, or a recovery code.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="code">Code</Label>
              <Input id="code" inputMode="text" autoComplete="one-time-code" required autoFocus
                value={code} onChange={(e) => setCode(e.target.value)} />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button type="submit" size="lg" className="w-full" disabled={busy}>
              {busy ? "Verifying…" : "Verify"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
