import { useCallback, useEffect, useMemo, useState } from 'react'
import { getCurrentUser, login as loginRequest } from '../api/agentguard'
import { onUnauthorized } from '../api/client'
import { clearToken, getToken, setToken } from '../api/session'
import type { CurrentUser } from '../api/types'
import { AuthContext, type AuthValue } from './context'

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null)

  // Derived at mount rather than set inside the effect: with no token there is
  // nothing to restore, so the app is not loading.
  const [loading, setLoading] = useState(() => getToken() !== null)

  // Restore a session from an existing tab token, so a refresh keeps you
  // signed in without persisting the user object anywhere.
  useEffect(() => {
    if (!getToken()) return

    let active = true

    getCurrentUser()
      .then((current) => {
        if (active) setUser(current)
      })
      .catch(() => {
        if (active) {
          clearToken()
          setUser(null)
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
    }
  }, [])

  // Any 401 from any call drops the session rather than leaving a dead UI.
  useEffect(() => onUnauthorized(() => setUser(null)), [])

  const signIn = useCallback(async (email: string, password: string) => {
    const response = await loginRequest(email, password)

    setToken(response.access_token)
    setUser(response.user)
  }, [])

  const signOut = useCallback(() => {
    clearToken()
    setUser(null)
  }, [])

  const value = useMemo<AuthValue>(
    () => ({
      user,
      loading,
      signIn,
      signOut,
      can: (permission) => user?.permissions.includes(permission) ?? false,
    }),
    [user, loading, signIn, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
