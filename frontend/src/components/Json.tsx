import type { Json } from '../api/types'

export function JsonBlock({ value }: { value: Json }) {
  return <pre className="json-body">{JSON.stringify(value, null, 2)}</pre>
}

export function JsonDetails({
  label,
  value,
  open = false,
}: {
  label: string
  value: Json
  open?: boolean
}) {
  return (
    <details className="json" open={open}>
      <summary>{label}</summary>
      <JsonBlock value={value} />
    </details>
  )
}

/** Compact `key = value` rendering for small tool argument objects. */
export function ArgumentList({ args }: { args: Record<string, Json> | null }) {
  const entries = Object.entries(args ?? {})

  if (entries.length === 0) {
    return <span className="faint mono">none</span>
  }

  return (
    <div className="arg-list">
      {entries.map(([key, value]) => (
        <div key={key}>
          <span className="arg-key">{key}</span> = {JSON.stringify(value)}
        </div>
      ))}
    </div>
  )
}
