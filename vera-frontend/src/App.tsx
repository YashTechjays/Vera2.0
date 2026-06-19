import { BrowserRouter, Routes, Route } from "react-router-dom"
import { AppShell } from "@/components/layout/AppShell"
import { LiveMonitoring } from "@/pages/LiveMonitoring"
import { DataManagement } from "@/pages/DataManagement"
import { Placeholder } from "@/pages/Placeholder"

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<LiveMonitoring />} />
          <Route path="data-management" element={<DataManagement />} />
          <Route
            path="call-history"
            element={<Placeholder title="Call History" />}
          />
          <Route path="analytics" element={<Placeholder title="Analytics" />} />
          <Route path="settings" element={<Placeholder title="Settings" />} />
          <Route path="*" element={<Placeholder title="Not Found" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

export default App
