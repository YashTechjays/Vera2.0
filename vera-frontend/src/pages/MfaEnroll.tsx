import { useState, type FormEvent } from "react"
import { Navigate, useNavigate } from "react-router-dom"
import { QRCodeSVG } from "qrcode.react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { apiErrorMessage } from "@/lib/api/client"
import { RecoveryCodes } from "@/components/auth/RecoveryCodes"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { selectMfa, selectTenantSlug, enrollActivateThunk } from "@/store/authSlice"

export function MfaEnroll() {
  const dispatch = useAppDispatch()
  const navigate = useNavigate()
  const mfa = useAppSelector(selectMfa)
  const slug = useAppSelector(selectTenantSlug) ?? ""
  const [code, setCode] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [recovery, setRecovery] = useState<string[] | null>(null)

  if (!mfa || mfa.step !== "enroll") return <Navigate to="/login" replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const codes = await dispatch(
        enrollActivateThunk({ slug, mfaToken: mfa!.token, code }),
      ).unwrap()
      setRecovery(codes)
    } catch (err) {
      setError(apiErrorMessage(err, "Enrollment failed."))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-lg">Set up two-factor authentication</CardTitle>
          <CardDescription>
            {recovery ? "Save your recovery codes to finish." : "Scan the QR code with your authenticator app, then enter a code."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {recovery ? (
            <RecoveryCodes codes={recovery} onContinue={() => navigate("/", { replace: true })} />
          ) : (
            <div className="space-y-4">
              {mfa.provisioningUri && (
                <div className="flex justify-center rounded-md bg-white p-4">
                  <QRCodeSVG value={mfa.provisioningUri} size={180} />
                </div>
              )}
              <form onSubmit={onSubmit} className="space-y-4">
                <div className="space-y-1.5">
                  <Label htmlFor="code">Authentication code</Label>
                  <Input id="code" inputMode="numeric" autoComplete="one-time-code" required autoFocus
                    value={code} onChange={(e) => setCode(e.target.value)} />
                </div>
                {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
                <Button type="submit" size="lg" className="w-full" disabled={busy}>
                  {busy ? "Activating…" : "Activate"}
                </Button>
              </form>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
