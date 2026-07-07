import { useState, type FormEvent } from "react"
import { Navigate, useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage } from "@/lib/api/errors"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import {
  platformVerifyMfaThunk,
  selectMfa,
  selectTenantSlug,
  verifyMfaThunk,
} from "@/store/authSlice"

export function MfaVerify() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const mfa = useAppSelector(selectMfa)
  const slug = useAppSelector(selectTenantSlug) ?? ""
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // No challenge in state (e.g. refresh) → back to login.
  if (!mfa || mfa.step !== "verify") return <Navigate to="/login" replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      // Platform challenges verify against the slug-less platform route.
      if (mfa!.platform) {
        await dispatch(platformVerifyMfaThunk({ mfaToken: mfa!.token, code })).unwrap()
      } else {
        await dispatch(verifyMfaThunk({ slug, mfaToken: mfa!.token, code })).unwrap()
      }
      navigate("/", { replace: true })
    } catch (err) {
      setError(apiErrorMessage(err, "Verification failed."))
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
