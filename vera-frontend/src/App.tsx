import { useEffect } from "react"
import { BrowserRouter, Routes, Route } from "react-router-dom"
import { AppShell } from "@/components/layout/AppShell"
import { RequireAuth } from "@/components/auth/RequireAuth"
import { useAppDispatch } from "@/store/hooks"
import { fetchMe } from "@/store/authSlice"
import { getToken } from "@/lib/auth/storage"
import { Login } from "@/pages/Login"
import { PlatformLogin } from "@/pages/PlatformLogin"
import { MfaVerify } from "@/pages/MfaVerify"
import { MfaEnroll } from "@/pages/MfaEnroll"
import { AcceptInvite } from "@/pages/AcceptInvite"
import { LiveMonitoring } from "@/pages/LiveMonitoring"
import { DataManagement } from "@/pages/DataManagement"
import { Users } from "@/pages/Users"
import { Settings } from "@/pages/Settings"
import { VoiceLab } from "@/pages/VoiceLab"
import { Placeholder } from "@/pages/Placeholder"

function App() {
  const dispatch = useAppDispatch()
  // Hydrate a persisted session once on mount.
  useEffect(() => {
    if (getToken()) dispatch(fetchMe())
  }, [dispatch])

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/platform/login" element={<PlatformLogin />} />
        <Route path="/mfa" element={<MfaVerify />} />
        <Route path="/mfa-enroll" element={<MfaEnroll />} />
        {/* Invite links are tenant-scoped (generated in the backend email). */}
        <Route path="/tenants/:tenantSlug/accept-invite" element={<AcceptInvite />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<LiveMonitoring />} />
            <Route path="data-management" element={<DataManagement />} />
            <Route path="users" element={<Users />} />
            <Route path="voice-lab" element={<VoiceLab />} />
            <Route path="call-history" element={<Placeholder title="Call History" />} />
            <Route path="analytics" element={<Placeholder title="Analytics" />} />
            {/* Super-admin-only stub; the persona/prompt editor lands here later. */}
            <Route path="agent-prompt" element={<Placeholder title="Agent Prompt" />} />
            <Route path="settings" element={<Settings />} />
            <Route path="*" element={<Placeholder title="Not Found" />} />
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
