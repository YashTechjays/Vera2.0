import { useState } from "react"
import { Outlet } from "react-router-dom"
import { Sidebar } from "./Sidebar"
import { Topbar } from "./Topbar"
import { IbvProvider } from "@/components/ibv/IbvProvider"
import { IbvFormModal } from "@/components/ibv/IbvFormModal"

export function AppShell() {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <IbvProvider>
      <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
        <Sidebar collapsed={collapsed} />
        <div className="flex min-w-0 flex-1 flex-col">
          <Topbar onToggleSidebar={() => setCollapsed((c) => !c)} />
          <main className="flex-1 overflow-y-auto p-6">
            <Outlet />
          </main>
        </div>
      </div>
      <IbvFormModal />
    </IbvProvider>
  )
}
