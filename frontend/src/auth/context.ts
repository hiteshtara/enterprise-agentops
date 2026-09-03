import { createContext, useContext } from 'react'
import type { CurrentUser, Permission } from '../api/types'

export interface AuthValue {
  user: CurrentUser | null
  loading: boolean
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => void
  /**
   * Whether the current user holds a permission.
   *
   * This drives what the console SHOWS. It is not security -- the backend
   * re-checks every request and is the only authority.
   */
  can: (permission: Permission) => boolean
}

export const AuthContext = createContext<AuthValue | null>(null)

export function useAuth(): AuthValue {
  const value = useContext(AuthContext)

  if (value === null) {
    throw new Error('useAuth must be used inside an AuthProvider.')
  }

  return value
}

/** Builds an AuthValue from a user, used by the provider and by tests. */
export function authValueFor(
  user: CurrentUser | null,
  overrides: Partial<AuthValue> = {},
): AuthValue {
  return {
    user,
    loading: false,
    signIn: async () => {},
    signOut: () => {},
    can: (permission) => user?.permissions.includes(permission) ?? false,
    ...overrides,
  }
}
