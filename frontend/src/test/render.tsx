import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { LocationProbe } from './LocationProbe'
import type { ReactElement } from 'react'
import { AuthContext, authValueFor } from '../auth/context'
import type { AuthValue } from '../auth/context'
import type { CurrentUser } from '../api/types'
import { adminUser } from './factories'

interface Options {
  route?: string
  /** Who is signed in. Defaults to an admin so tests opt in to restriction. */
  user?: CurrentUser | null
  auth?: Partial<AuthValue>
}

export function renderWithRouter(
  ui: ReactElement,
  routeOrOptions: string | Options = {},
  maybeOptions: Options = {},
) {
  const options: Options =
    typeof routeOrOptions === 'string'
      ? { route: routeOrOptions, ...maybeOptions }
      : routeOrOptions

  const { route = '/', user = adminUser, auth } = options

  return render(
    <AuthContext.Provider value={authValueFor(user, auth)}>
      <MemoryRouter initialEntries={[route]}>
        {ui}
        <LocationProbe />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}
