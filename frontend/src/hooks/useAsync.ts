import { useCallback, useEffect, useState } from 'react'

interface AsyncState<T> {
  data: T | null
  error: unknown
  loading: boolean
}

/** Runs an async loader on mount and exposes a reload for refresh actions. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: true,
  })

  // The loader is redefined every render by callers, so the dependency list is
  // supplied explicitly rather than inferred from the function identity.
  const run = useCallback(() => {
    let active = true

    setState((previous) => ({ ...previous, loading: true, error: null }))

    loader()
      .then((data) => {
        if (active) setState({ data, error: null, loading: false })
      })
      .catch((error: unknown) => {
        if (active) setState({ data: null, error, loading: false })
      })

    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => run(), [run])

  return { ...state, reload: run }
}
