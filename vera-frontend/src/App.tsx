import { useEffect } from "react"
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
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

const DEFAULT_SLUG = import.meta.env.VITE_DEFAULT_TENANT_SLUG ?? ""

function App() {
  const dispatch = useAppDispatch()
  // Hydrate a persisted session once on mount.
  useEffect(() => {
    if (getToken()) dispatch(fetchMe())
  }, [dispatch])

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Navigate to={`/tenants/${DEFAULT_SLUG}/login`} replace />} />
        <Route path="/tenants/:tenantSlug/login" element={<Login />} />
        <Route path="/tenants/:tenantSlug/mfa" element={<MfaVerify />} />
        <Route path="/tenants/:tenantSlug/mfa-enroll" element={<MfaEnroll />} />
        <Route path="/tenants/:tenantSlug/accept-invite" element={<AcceptInvite />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<LiveMonitoring />} />
            <Route path="data-management" element={<DataManagement />} />
            <Route path="users" element={<Users />} />
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
