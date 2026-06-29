import { lazy, useEffect } from "react"
import { BrowserRouter, Routes, Route } from "react-router-dom"
import { AppShell } from "@/components/layout/AppShell"
import { RequireAuth } from "@/components/auth/RequireAuth"
import { useAppDispatch } from "@/store/hooks"
import { fetchMe } from "@/store/authSlice"
import { getToken } from "@/lib/auth/storage"
import { Login } from "@/pages/Login"
import { MfaVerify } from "@/pages/MfaVerify"
import { MfaEnroll } from "@/pages/MfaEnroll"
import { AcceptInvite } from "@/pages/AcceptInvite"
import { LiveMonitoring } from "@/pages/LiveMonitoring"
import { DataManagement } from "@/pages/DataManagement"
import { Users } from "@/pages/Users"
import { Settings } from "@/pages/Settings"
import { Placeholder } from "@/pages/Placeholder"

// Lazy-loaded: Voice Lab pulls in livekit-client + react-phone-number-input's
// "max" libphonenumber metadata (the largest set), so code-split it out of the
// initial bundle — only operators who open the page pay for it.
const VoiceLab = lazy(() =>
  import("@/pages/VoiceLab").then((m) => ({ default: m.VoiceLab })),
)

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
        <Route path="/mfa" element={<MfaVerify />} />
        <Route path="/mfa-enroll" element={<MfaEnroll />} />
        {/* Invite links are tenant-scoped (generated in the backend email). */}
        <Route path="/tenants/:tenantSlug/accept-invite" element={<AcceptInvite />} />
        <Route element={<RequireAuth />}>
          {/* AppShell owns the Suspense boundary around its <Outlet>, so a lazy
              page (e.g. Voice Lab) shows a spinner in the content area while the
              sidebar/topbar stay mounted. */}
          <Route element={<AppShell />}>
            <Route index element={<LiveMonitoring />} />
            <Route path="data-management" element={<DataManagement />} />
            <Route path="users" element={<Users />} />
            <Route path="voice-lab" element={<VoiceLab />} />
            <Route path="call-history" element={<Placeholder title="Call History" />} />
            <Route path="analytics" element={<Placeholder title="Analytics" />} />
            <Route path="settings" element={<Settings />} />
            <Route path="*" element={<Placeholder title="Not Found" />} />
          </Route>
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
