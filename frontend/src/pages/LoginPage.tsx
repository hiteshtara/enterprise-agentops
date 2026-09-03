import { useState } from 'react'
import { useAuth } from '../auth/context'
import { ErrorState } from '../components/States'

const DEMO_ACCOUNTS = [
  {
    role: 'Viewer',
    email: 'viewer@agentguard.local',
    password: 'viewer-demo-password',
  },
  {
    role: 'Operator',
    email: 'operator@agentguard.local',
    password: 'operator-demo-password',
  },
  {
    role: 'Approver',
    email: 'approver@agentguard.local',
    password: 'approver-demo-password',
  },
  { role: 'Admin', email: 'admin@agentguard.local', password: 'admin-demo-password' },
]

export function LoginPage() {
  const { signIn } = useAuth()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()

    if (busy) return

    setBusy(true)
    setError(null)

    try {
      await signIn(email, password)
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <div className="login-card">
        <div className="brand-name" style={{ fontSize: 18 }}>
          <svg
            className="brand-mark"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
          >
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
          AgentGuard
        </div>

        <p className="page-subtitle" style={{ marginBottom: 22 }}>
          The control plane for AI agents that touch real business systems.
        </p>

        <form onSubmit={submit} className="stack" style={{ gap: 12 }}>
          <div className="field">
            <label className="field-label" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              disabled={busy}
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              disabled={busy}
            />
          </div>

          {error ? <ErrorState error={error} /> : null}

          <button
            type="submit"
            className="primary"
            disabled={busy || !email || !password}
          >
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <div className="demo-note">
          <strong>Demo accounts — local development only.</strong> These credentials are
          published in the repository and are not secrets.
          <table style={{ marginTop: 10 }}>
            <tbody>
              {DEMO_ACCOUNTS.map((account) => (
                <tr key={account.email}>
                  <td style={{ padding: '4px 0', border: 0 }}>{account.role}</td>
                  <td className="mono" style={{ padding: '4px 0', border: 0 }}>
                    {account.email}
                  </td>
                  <td className="mono faint" style={{ padding: '4px 0', border: 0 }}>
                    {account.password}
                  </td>
                  <td style={{ padding: '4px 0', border: 0, textAlign: 'right' }}>
                    <button
                      type="button"
                      onClick={() => {
                        setEmail(account.email)
                        setPassword(account.password)
                      }}
                    >
                      Use
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
