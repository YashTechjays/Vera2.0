import { MfaSetupDialog } from "@/components/auth/MfaSetupDialog"
import { useAppSelector } from "@/store/hooks"
import { selectUser } from "@/store/authSlice"

export function Settings() {
  const user = useAppSelector(selectUser)
  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">Signed in as {user?.email}</p>
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-medium">Security</h2>
        <p className="text-sm text-muted-foreground">
          Two-factor authentication adds a one-time code at sign-in for extra protection.
        </p>
        <MfaSetupDialog />
      </section>
    </div>
  )
}
