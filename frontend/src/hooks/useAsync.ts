import { useCallback, useEffect, useRef, useState } from 'react'

export interface AsyncOptions<T> {
  /** How often to refetch while `pollWhile` holds. Omit to disable polling. */
  intervalMs?: number
  /** Poll only while this returns true for the latest data. */
  pollWhile?: (data: T) => boolean
}

interface AsyncState<T> {
  data: T | null
  error: unknown
  loading: boolean
  refreshing: boolean
}

/**
 * Loads data on mount, with an optional poll while a condition holds.
 *
 * A background poll sets `refreshing`, not `loading`, so the page never flashes
 * a spinner over content the user is already reading.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: AsyncOptions<T> = {},
) {
  const { intervalMs, pollWhile } = options

  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: true,
    refreshing: false,
  })

  const alive = useRef(true)
  const latest = useRef<T | null>(null)

  useEffect(() => {
    alive.current = true

    return () => {
      alive.current = false
    }
  }, [])

  // The loader closes over render-scope values, so callers pass `deps`
  // explicitly rather than relying on its identity.
  const run = useCallback(
    async (background = false) => {
      setState((previous) => ({
        ...previous,
        loading: background ? previous.loading : true,
        refreshing: background,
        error: background ? previous.error : null,
      }))

      try {
        const data = await loader()

        if (!alive.current) return

        latest.current = data

        setState({ data, error: null, loading: false, refreshing: false })
      } catch (error) {
        if (!alive.current) return

        // A failed background poll keeps the last good data on screen.
        setState((previous) =>
          background
            ? { ...previous, refreshing: false, error }
            : { data: null, error, loading: false, refreshing: false },
        )
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps,
  )

  useEffect(() => {
    void run()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run])

  useEffect(() => {
    if (!intervalMs) return

    const timer = window.setInterval(() => {
      // Skip while the tab is hidden, and stop once the condition is false.
      if (document.visibilityState === 'hidden') return
      if (latest.current === null) return
      if (pollWhile && !pollWhile(latest.current)) return

      void run(true)
    }, intervalMs)

    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, run])

  return { ...state, reload: () => run(false) }
}
