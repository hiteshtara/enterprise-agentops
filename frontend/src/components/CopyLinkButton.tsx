import { useCopyToClipboard } from '../hooks/useCopyToClipboard'

/**
 * Copies a deep link to the current page.
 *
 * The link is not a share token: it opens the same authenticated route, so a
 * recipient still has to sign in and still needs the relevant permission.
 */
export function CopyLinkButton({
  label = 'Copy run link',
  url,
}: {
  label?: string
  url?: string
}) {
  const { status, copy } = useCopyToClipboard()

  const target = url ?? (typeof window === 'undefined' ? '' : window.location.href)

  return (
    <span className="row" style={{ gap: 8 }}>
      <button type="button" onClick={() => copy(target)} aria-label={label}>
        {label}
      </button>

      {/* Announced to screen readers when it appears. */}
      <span className="copy-feedback" role="status" aria-live="polite">
        {status === 'copied' ? 'Link copied' : null}
        {status === 'failed' ? 'Could not copy link' : null}
      </span>
    </span>
  )
}
