import { Route, Routes } from 'react-router-dom'
import { getOverview } from './api/agentguard'
import { Layout } from './components/Layout'
import { useAsync } from './hooks/useAsync'
import { AgentPage } from './pages/AgentPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { AuditPage } from './pages/AuditPage'
import { OverviewPage } from './pages/OverviewPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { RunsPage } from './pages/RunsPage'
import { ToolsPage } from './pages/ToolsPage'

export function App() {
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
