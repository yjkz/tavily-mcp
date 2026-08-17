import { useEffect, useState } from 'react'
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import { Toaster } from '@/components/ui/sonner'
import { Skeleton } from '@/components/ui/skeleton'
import Layout from '@/components/Layout'
import Login from '@/pages/Login'
import OverviewPage from '@/pages/Overview'
import KeysPage from '@/pages/Keys'
import TokensPage from '@/pages/Tokens'
import LogsPage from '@/pages/Logs'
import SettingsPage from '@/pages/Settings'
import { api } from '@/api'

function RequireAuth({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<'checking' | 'ok'>('checking')
  useEffect(() => {
    api
      .session()
      .then(() => setState('ok'))
      .catch(() => {
        window.location.hash = '#/login'
      })
  }, [])
  if (state === 'checking') {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Skeleton className="h-8 w-40" />
      </div>
    )
  }
  return <>{children}</>
}

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route index element={<OverviewPage />} />
          <Route path="keys" element={<KeysPage />} />
          <Route path="tokens" element={<TokensPage />} />
          <Route path="logs" element={<LogsPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      <Toaster richColors position="top-center" theme="dark" />
    </HashRouter>
  )
}
