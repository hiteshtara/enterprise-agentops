import { useCallback, useEffect, useRef, useState } from 'react'

const RESET_AFTER_MS = 2000

/**
 * Copies text and reports the outcome, resetting after a moment.
 *
 * Falls back to a hidden textarea + execCommand where the async Clipboard API
 * is unavailable (older browsers, or a non-secure origin).
 */
export function useCopyToClipboard() {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<number | undefined>(undefined)

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current)
    },
    [],
  )

  const copy = useCallback(async (text: string) => {
    let ok = false

    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
        ok = true
      }
    } catch {
      ok = false
    }

    if (!ok) {
      try {
        const field = document.createElement('textarea')

        field.value = text
        field.setAttribute('readonly', '')
        field.style.position = 'fixed'
        field.style.opacity = '0'

        document.body.appendChild(field)
        field.select()

        ok = document.execCommand('copy')

        document.body.removeChild(field)
      } catch {
        ok = false
      }
    }

    setStatus(ok ? 'copied' : 'failed')

    if (timer.current) window.clearTimeout(timer.current)

    timer.current = window.setTimeout(() => setStatus('idle'), RESET_AFTER_MS)

    return ok
  }, [])

  return { status, copy }
}
