import { Route, Routes } from 'react-router-dom'
import { getOverview } from './api/agentguard'
import { useAuth } from './auth/context'
import { Layout } from './components/Layout'
import { Loading } from './components/States'
import { useAsync } from './hooks/useAsync'
import { AgentPage } from './pages/AgentPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { AuditPage } from './pages/AuditPage'
import { LoginPage } from './pages/LoginPage'
import { OverviewPage } from './pages/OverviewPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { RunsPage } from './pages/RunsPage'
import { ToolsPage } from './pages/ToolsPage'

function Console() {
  // Drives the pending-approval count in the sidebar. A failure here must not
  // take down navigation, so the error is intentionally ignored.
  const { data } = useAsync(getOverview, [])

  return (
    <Layout pendingApprovals={data?.pending_approvals}>
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/agent" element={<AgentPage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:runId" element={<RunDetailPage />} />
        <Route path="/approvals" element={<ApprovalsPage />} />
        <Route path="/audit" element={<AuditPage />} />
        <Route path="/tools" element={<ToolsPage />} />
      </Routes>
    </Layout>
  )
}

export function App() {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="login-shell">
        <Loading label="Restoring session" />
      </div>
    )
  }

  // Every console route sits behind a signed-in user. The backend enforces
  // this independently; this only decides what is rendered.
  return user ? <Console /> : <LoginPage />
}
