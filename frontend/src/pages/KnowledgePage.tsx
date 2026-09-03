import { useCallback, useState } from 'react'
import {
  createKnowledge,
  decideKnowledge,
  editKnowledge,
  listKnowledge,
  supersedeKnowledge,
} from '../api/agentguard'
import type { KnowledgeAudience, KnowledgeItem, KnowledgeStatus } from '../api/types'
import { useAuth } from '../auth/context'
import { useAsync } from '../hooks/useAsync'
import { useUrlFilter } from '../hooks/useUrlFilter'
import { PageHeader } from '../components/Layout'
import { Empty, ErrorState, Loading } from '../components/States'

const STATUSES: KnowledgeStatus[] = ['PROPOSED', 'APPROVED', 'REJECTED', 'SUPERSEDED']

const NUMERIC_WARNING = 'Contains a potentially stale operational number.'

const INTERNAL_WARNING = 'Not used in guest replies.'

/**
 * A rough hint that a candidate carries more than one rule.
 *
 * Distillation occasionally merges two rules into one sentence pair. This is a
 * word count and a sentence count, nothing cleverer -- the owner reads the text
 * anyway, and a wrong guess here would train them to ignore the badge.
 */
function looksLikeTwoRules(content: string): boolean {
  const sentences = content.split(/[.!?]+\s/).filter((part) => part.trim().length > 0)

  return sentences.length >= 3 && content.split(/\s+/).length > 45
}

function SafetyNote({ item }: { item: KnowledgeItem }) {
  if (item.safety_status !== 'REVIEW_NUMERIC_FACT') return null

  const kinds = item.safety_reasons
    .filter((reason) => reason.startsWith('numeric:'))
    .map((reason) => reason.slice('numeric:'.length))

  return (
    <div className="knowledge-warning" role="note">
      <strong>Check this number</strong>
      <div>
        {NUMERIC_WARNING}
        {kinds.length > 0 ? ` Flagged: ${kinds.join(', ')}.` : ''} Confirm it is still
        true before approving.
      </div>
    </div>
  )
}

function AudienceNote({ item }: { item: KnowledgeItem }) {
  if (item.audience !== 'INTERNAL_OPERATION') return null

  return (
    <div className="knowledge-internal" role="note">
      <strong>Internal operations — {INTERNAL_WARNING}</strong>
      <div>
        Approving this keeps it for reference. It stays out of guest-facing drafting
        even once approved.
      </div>
    </div>
  )
}

function Meta({ item }: { item: KnowledgeItem }) {
  const observed =
    item.first_observed_at && item.last_observed_at
      ? `${item.first_observed_at.slice(0, 10)} → ${item.last_observed_at.slice(0, 10)}`
      : null

  return (
    <div className="knowledge-meta faint">
      <span>{item.topic}</span>
      <span>·</span>
      <span>{item.property_slug ?? 'GLOBAL'}</span>
      <span>·</span>
      <span>
        {item.evidence_count} repl{item.evidence_count === 1 ? 'y' : 'ies'} across{' '}
        {item.evidence_property_count} propert
        {item.evidence_property_count === 1 ? 'y' : 'ies'}
      </span>
      {observed ? (
        <>
          <span>·</span>
          <span>{observed}</span>
        </>
      ) : null}
      <span>·</span>
      <span>{item.created_at.slice(0, 10)}</span>
    </div>
  )
}

function KnowledgeCard({
  item,
  busy,
  onDecide,
  onSave,
  onSupersede,
}: {
  item: KnowledgeItem
  busy: boolean
  onDecide: (ref: string, decision: 'approve' | 'reject') => Promise<void>
  onSave: (ref: string, content: string) => Promise<void>
  onSupersede: (ref: string, content: string) => Promise<void>
}) {
  const { can } = useAuth()

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(item.content)

  // Mirrors the backend policy so the console does not offer an action that
  // would be refused. The backend still decides.
  const mayDecide = can('ADMINISTER')

  const proposed = item.status === 'PROPOSED'
  const approved = item.status === 'APPROVED'

  async function save() {
    await (proposed ? onSave : onSupersede)(item.knowledge_ref, draft)

    setEditing(false)
  }

  return (
    <section className="card knowledge-card" aria-label={item.title}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <strong>{item.title}</strong>
        <div className="row">
          <span
            className={
              item.audience === 'GUEST_FACING'
                ? 'badge tone-info'
                : 'badge tone-neutral'
            }
          >
            <span className="badge-dot" aria-hidden="true" />
            {item.audience === 'GUEST_FACING' ? 'Guest-facing' : 'Internal'}
          </span>
          <span className="badge tone-neutral">
            <span className="badge-dot" aria-hidden="true" />
            {item.status}
          </span>
        </div>
      </div>

      <Meta item={item} />

      <AudienceNote item={item} />
      <SafetyNote item={item} />

      {looksLikeTwoRules(item.content) ? (
        <div className="knowledge-warning" role="note">
          <strong>This may contain more than one rule</strong>
          <div>Consider editing it down to the single rule you want to keep.</div>
        </div>
      ) : null}

      {editing ? (
        <>
          <label className="field-label" htmlFor={`edit-${item.knowledge_ref}`}>
            Rule
          </label>
          <textarea
            id={`edit-${item.knowledge_ref}`}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={busy}
          />
        </>
      ) : (
        <div className="knowledge-content">{item.content}</div>
      )}

      {mayDecide ? (
        <div className="approval-actions">
          {editing ? (
            <>
              <button
                type="button"
                className="primary"
                disabled={busy || !draft.trim()}
                onClick={save}
              >
                {approved ? 'Save as new version' : 'Save'}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setDraft(item.content)
                  setEditing(false)
                }}
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              {proposed ? (
                <>
                  <button
                    type="button"
                    className="approve"
                    disabled={busy}
                    onClick={() => onDecide(item.knowledge_ref, 'approve')}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="reject"
                    disabled={busy}
                    onClick={() => onDecide(item.knowledge_ref, 'reject')}
                  >
                    Reject
                  </button>
                </>
              ) : null}
              {proposed || approved ? (
                <button type="button" disabled={busy} onClick={() => setEditing(true)}>
                  Edit
                </button>
              ) : null}
            </>
          )}
        </div>
      ) : (
        <p className="not-permitted">
          Your role can read knowledge but not change it. Deciding requires the{' '}
          <span className="mono">ADMINISTER</span> permission.
        </p>
      )}
    </section>
  )
}

function AddKnowledge({
  busy,
  onCreate,
}: {
  busy: boolean
  onCreate: (payload: {
    property_slug: string | null
    topic: string
    title: string
    content: string
    audience: KnowledgeAudience
  }) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [topic, setTopic] = useState('')
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [slug, setSlug] = useState('')
  const [audience, setAudience] = useState<KnowledgeAudience>('GUEST_FACING')

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Add knowledge
      </button>
    )
  }

  return (
    <section className="card" aria-label="Add knowledge">
      <div className="approval-term" style={{ marginBottom: 8 }}>
        Add knowledge
      </div>

      <p className="faint" style={{ fontSize: 12, marginTop: 0 }}>
        A rule you write yourself is approved immediately — you are the review.
      </p>

      <label className="field-label" htmlFor="new-title">
        Title
      </label>
      <input id="new-title" value={title} onChange={(e) => setTitle(e.target.value)} />

      <label className="field-label" htmlFor="new-topic" style={{ marginTop: 12 }}>
        Topic
      </label>
      <input id="new-topic" value={topic} onChange={(e) => setTopic(e.target.value)} />

      <label className="field-label" htmlFor="new-slug" style={{ marginTop: 12 }}>
        Property slug (leave blank for GLOBAL)
      </label>
      <input id="new-slug" value={slug} onChange={(e) => setSlug(e.target.value)} />

      <label className="field-label" htmlFor="new-audience" style={{ marginTop: 12 }}>
        Audience
      </label>
      <select
        id="new-audience"
        value={audience}
        onChange={(e) => setAudience(e.target.value as KnowledgeAudience)}
      >
        <option value="GUEST_FACING">Guest-facing</option>
        <option value="INTERNAL_OPERATION">Internal operations</option>
      </select>

      <label className="field-label" htmlFor="new-content" style={{ marginTop: 12 }}>
        Rule
      </label>
      <textarea
        id="new-content"
        value={content}
        onChange={(e) => setContent(e.target.value)}
      />

      <div className="approval-actions">
        <button
          type="button"
          className="primary"
          disabled={busy || !title.trim() || !topic.trim() || !content.trim()}
          onClick={async () => {
            await onCreate({
              property_slug: slug.trim() || null,
              topic: topic.trim(),
              title: title.trim(),
              content,
              audience,
            })

            setOpen(false)
            setTitle('')
            setTopic('')
            setContent('')
            setSlug('')
          }}
        >
          Create
        </button>
        <button type="button" disabled={busy} onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </section>
  )
}

export function KnowledgePage() {
  const { can } = useAuth()

  // The filter lives in the URL so a review session is shareable and survives
  // a reload, like every other filter in this console. An absent parameter
  // means the review queue, which is what someone opening this page wants.
  const [selected, setStatus] = useUrlFilter<KnowledgeStatus>('status', STATUSES)

  const status: KnowledgeStatus = selected || 'PROPOSED'

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const page = useAsync(
    useCallback(() => listKnowledge({ status }), [status]),
    [status],
  )

  const items = page.data?.items ?? []
  const counts = page.data?.counts
  const conflicts = page.data?.conflicts ?? []

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)

    try {
      await action()
      // Re-read rather than splicing local state, so the screen shows what was
      // actually persisted.
      page.reload()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
    }
  }

  const guestFacing = items.filter((item) => item.audience === 'GUEST_FACING')

  // Three sections over one dataset -- the same rows, grouped, never duplicated.
  const sections =
    status === 'PROPOSED'
      ? [
          {
            key: 'ready',
            title: 'Ready for review',
            hint: 'Guest-facing, no perishable numbers.',
            rows: guestFacing.filter((item) => item.safety_status === 'SAFE'),
          },
          {
            key: 'numeric',
            title: 'Numeric / stale-fact review',
            hint: 'Useful, but each contains a number nobody has confirmed lately.',
            rows: guestFacing.filter(
              (item) => item.safety_status === 'REVIEW_NUMERIC_FACT',
            ),
          },
          {
            key: 'internal',
            title: 'Internal operations',
            hint: 'Kept for reference. Never used in guest replies.',
            rows: items.filter((item) => item.audience === 'INTERNAL_OPERATION'),
          },
        ]
      : [{ key: 'all', title: '', hint: '', rows: items }]

  return (
    <>
      <PageHeader
        title="Knowledge"
        subtitle="What Priyanka Homes says. Only approved, guest-facing rules reach a guest reply."
        actions={
          can('ADMINISTER') ? (
            <AddKnowledge
              busy={busy}
              onCreate={(payload) => run(() => createKnowledge(payload))}
            />
          ) : null
        }
      />

      <div className="row" style={{ marginBottom: 16 }} role="tablist">
        {STATUSES.map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={status === value}
            className={status === value ? 'primary' : undefined}
            onClick={() => setStatus(value)}
          >
            {value}
            {counts?.[value] !== undefined ? ` (${counts[value]})` : ''}
          </button>
        ))}
      </div>

      {conflicts.length > 0 ? (
        <div className="state state-warn" role="alert" style={{ marginBottom: 16 }}>
          <strong>
            {conflicts.length} approved-knowledge conflict
            {conflicts.length === 1 ? '' : 's'}
          </strong>
          <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {conflicts.map((conflict) => (
              <li key={`${conflict.scope}-${conflict.topic}-${conflict.reason}`}>
                <span className="mono">{conflict.topic}</span> ·{' '}
                <span className="mono">{conflict.scope}</span> — {conflict.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {error ? <ErrorState error={error} /> : null}
      {page.error && !page.data ? <ErrorState error={page.error} /> : null}
      {page.loading ? <Loading label="Loading knowledge" /> : null}

      {!page.loading && items.length === 0 && !page.error ? (
        <Empty message={`No ${status.toLowerCase()} knowledge.`} />
      ) : null}

      {sections.map((section) =>
        section.rows.length === 0 ? null : (
          <div key={section.key} style={{ marginBottom: 24 }}>
            {section.title ? (
              <>
                <div className="approval-term">
                  {section.title} ({section.rows.length})
                </div>
                <p className="faint" style={{ fontSize: 12, marginTop: 2 }}>
                  {section.hint}
                </p>
              </>
            ) : null}

            <div className="stack">
              {section.rows.map((item) => (
                <KnowledgeCard
                  key={item.knowledge_ref}
                  item={item}
                  busy={busy}
                  onDecide={(ref, decision) =>
                    run(() => decideKnowledge(ref, decision))
                  }
                  onSave={(ref, content) => run(() => editKnowledge(ref, { content }))}
                  onSupersede={(ref, content) =>
                    run(() => supersedeKnowledge(ref, { content }))
                  }
                />
              ))}
            </div>
          </div>
        ),
      )}
    </>
  )
}
