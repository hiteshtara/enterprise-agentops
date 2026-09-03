import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * A filter value backed by a URL search parameter.
 *
 * The URL is the single source of truth, so a refresh keeps the filter, the
 * link is shareable, and browser back/forward step through filter changes
 * naturally (updates push rather than replace).
 *
 * `allowed` guards against a hand-edited URL: an unrecognised value reads as
 * "no filter" instead of being sent to the backend.
 */
export function useUrlFilter<T extends string>(
  key: string,
  allowed?: readonly T[],
): [T | '', (next: T | '') => void] {
  const [params, setParams] = useSearchParams()

  const raw = params.get(key) ?? ''

  const value = raw && allowed && !allowed.includes(raw as T) ? '' : (raw as T | '')

  const setValue = useCallback(
    (next: T | '') => {
      setParams(
        (previous) => {
          const copy = new URLSearchParams(previous)

          if (next) copy.set(key, next)
          else copy.delete(key)

          return copy
        },
        { replace: false },
      )
    },
    [key, setParams],
  )

  return [value, setValue]
}
