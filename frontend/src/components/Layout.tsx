import { NavLink } from 'react-router-dom'
import { useAuth } from '../auth/context'
import type { Permission } from '../api/types'

interface NavItem {
  to: string
  label: string
  end: boolean
  /** Hides the link when absent. UI hiding is convenience, not security. */
  permission?: Permission
}

const NAV: NavItem[] = [
  { to: '/', label: 'Overview', end: true, permission: 'VIEW_RUNS' },
  { to: '/agent', label: 'Agent', end: false, permission: 'RUN_AGENT' },
  { to: '/runs', label: 'Runs', end: false, permission: 'VIEW_RUNS' },
  { to: '/approvals', label: 'Approvals', end: false, permission: 'VIEW_APPROVALS' },
  { to: '/audit', label: 'Audit', end: false, permission: 'VIEW_AUDIT' },
  { to: '/tools', label: 'Tools', end: false, permission: 'VIEW_TOOLS' },
]

function Shield() {
  return (
    <svg className="brand-mark" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 2.5 4.5 5.6v6.1c0 4.6 3.1 8.4 7.5 9.8 4.4-1.4 7.5-5.2 7.5-9.8V5.6L12 2.5Z"
        stroke="#3d7dff"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path
        d="m8.7 12 2.3 2.3 4.3-4.6"
        stroke="#3d7dff"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function Layout({
  children,
  pendingApprovals,
}: {
  children: React.ReactNode
  pendingApprovals?: number
}) {
  const { user, signOut, can } = useAuth()

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-name">
            <Shield />
            AgentGuard
          </div>
          <p className="brand-tagline">
            The control plane for AI agents that touch real business systems.
          </p>
        </div>

        <nav className="nav" aria-label="Primary">
          <div className="nav-label">Control plane</div>
          {NAV.filter((item) => !item.permission || can(item.permission)).map(
            (item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
              >
                {item.label}
                {item.label === 'Approvals' && pendingApprovals ? (
                  <span className="nav-count">{pendingApprovals}</span>
                ) : null}
              </NavLink>
            ),
          )}
        </nav>

        {user ? (
          <div className="identity">
            <div className="identity-name">{user.display_name}</div>
            <div className="identity-email faint">{user.email}</div>
            <div
              className="row"
              style={{ justifyContent: 'space-between', marginTop: 8 }}
            >
              <span className="badge tone-info">
                <span className="badge-dot" aria-hidden="true" />
                {user.role}
              </span>
              <button type="button" onClick={signOut}>
                Sign out
              </button>
            </div>
          </div>
        ) : null}
      </aside>

      <main className="main">{children}</main>
    </div>
  )
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: string
  actions?: React.ReactNode
}) {
  return (
    <header className="page-header">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div>
          <h1 className="page-title">{title}</h1>
          {subtitle ? <p className="page-subtitle">{subtitle}</p> : null}
        </div>
        {actions}
      </div>
    </header>
  )
}
