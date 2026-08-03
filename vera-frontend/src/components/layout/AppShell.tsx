import { Suspense, useEffect, useState } from "react"
import { Loader2 } from "lucide-react"
import { Outlet } from "react-router-dom"
import { Sidebar } from "./Sidebar"
import { Topbar } from "./Topbar"
import { IbvProvider } from "@/components/ibv/IbvProvider"
import { IbvFormModal } from "@/components/ibv/IbvFormModal"
import { CreatePatientFormModal } from "@/components/ibv/CreatePatientFormModal"
import { IdleManager } from "@/components/auth/IdleManager"
import { useAppDispatch, useAppSelector } from "@/store/hooks"
import { fetchMe, selectActiveElevation } from "@/store/authSlice"

export function AppShell() {
  const [collapsed, setCollapsed] = useState(false)
  const dispatch = useAppDispatch()
  const elevation = useAppSelector(selectActiveElevation)

  // When a platform operator's elevation lapses, re-fetch /auth/me so the sidebar
  // hides the tenant menus again (the grant is server-side; this just re-syncs).
  useEffect(() => {
    if (!elevation) return
    const ms = Date.parse(elevation.expires_at) - Date.now()
    if (ms <= 0) {
      void dispatch(fetchMe())
      return
    }
    const timer = setTimeout(() => void dispatch(fetchMe()), ms)
    return () => clearTimeout(timer)
  }, [elevation, dispatch])

  return (
    <IbvProvider>
      <IdleManager />
      <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
        <Sidebar collapsed={collapsed} />
        <div className="flex min-w-0 flex-1 flex-col">
          <Topbar onToggleSidebar={() => setCollapsed((c) => !c)} />
          <main className="flex-1 overflow-y-auto p-6">
            {/* Boundary for lazy route pages — only the content area shows the
                spinner; the sidebar/topbar above stay mounted. */}
            <Suspense
              fallback={
                <div className="flex h-full items-center justify-center">
                  <Loader2 className="size-5 animate-spin text-muted-foreground" />
                </div>
              }
            >
              <Outlet />
            </Suspense>
          </main>
        </div>
      </div>
      <IbvFormModal />
      <CreatePatientFormModal />
    </IbvProvider>
  )
}
