import { listTools } from '../api/agentguard'
import { useAsync } from '../hooks/useAsync'
import { RiskBadge } from '../components/Badges'
import { JsonDetails } from '../components/Json'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

const RISK_NOTE: Record<string, string> = {
  READ: 'Executes immediately',
  WRITE: 'Requires human approval',
  DANGEROUS: 'Requires human approval',
}

export function ToolsPage() {
  const { data, error, loading } = useAsync(listTools, [])

  return (
    <>
      <PageHeader
        title="Tools"
        subtitle="Every capability the agent can propose, and how each is governed."
      />

      {loading ? <Loading label="Loading tools" /> : null}
      {error ? <ErrorState error={error} /> : null}

      {data && data.length === 0 ? <Empty message="No tools registered." /> : null}

      {data && data.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Tool</th>
                <th>Description</th>
                <th>Risk</th>
                <th>Policy</th>
                <th>Schema</th>
              </tr>
            </thead>
            <tbody>
              {data.map((tool) => (
                <tr key={tool.name}>
                  <td className="mono">{tool.name}</td>
                  <td className="muted" style={{ maxWidth: 340 }}>
                    {tool.description}
                  </td>
                  <td>
                    <RiskBadge risk={tool.risk} />
                  </td>
                  <td className="muted">{RISK_NOTE[tool.risk] ?? '—'}</td>
                  <td style={{ minWidth: 200 }}>
                    <JsonDetails label="Parameter schema" value={tool.parameters} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  )
}
