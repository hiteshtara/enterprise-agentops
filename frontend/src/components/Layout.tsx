import { NavLink } from 'react-router-dom'

const NAV = [
  { to: '/', label: 'Overview', end: true },
  { to: '/agent', label: 'Agent', end: false },
  { to: '/runs', label: 'Runs', end: false },
  { to: '/approvals', label: 'Approvals', end: false },
  { to: '/audit', label: 'Audit', end: false },
  { to: '/tools', label: 'Tools', end: false },
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
          {NAV.map((item) => (
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
          ))}
        </nav>
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
