import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { App } from '../App'
import { AuthProvider } from './AuthContext'
import * as api from '../api/agentguard'
import { ApiError } from '../api/client'
import { clearToken, getToken, setToken } from '../api/session'
import { approverUser, operatorUser, overview, viewerUser } from '../test/factories'

vi.mock('../api/agentguard')

function renderApp() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('authentication', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    clearToken()
    vi.mocked(api.getOverview).mockResolvedValue(overview)
  })

  it('shows the login screen when nobody is signed in', async () => {
    renderApp()

    expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('marks the demo credentials as local-only', async () => {
    renderApp()

    expect(await screen.findByText(/local development only/i)).toBeInTheDocument()
  })

  it('signs in and stores the token for the tab', async () => {
    vi.mocked(api.login).mockResolvedValue({
      access_token: 'a-token',
      token_type: 'bearer',
      user: operatorUser,
    })

    renderApp()

    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Email'), operatorUser.email)
    await user.type(screen.getByLabelText('Password'), 'operator-demo-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() => {
      expect(api.login).toHaveBeenCalledWith(
        operatorUser.email,
        'operator-demo-password',
      )
    })

    expect(await screen.findByRole('navigation')).toBeInTheDocument()
    expect(getToken()).toBe('a-token')
  })

  it('shows a safe message when the credentials are wrong', async () => {
    vi.mocked(api.login).mockRejectedValue(
      new ApiError('Incorrect email or password.', 401),
    )

    renderApp()

    const user = userEvent.setup()

    await user.type(await screen.findByLabelText('Email'), 'admin@agentguard.local')
    await user.type(screen.getByLabelText('Password'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Incorrect email or password.',
    )
    expect(getToken()).toBeNull()
  })

  it('restores a session from an existing token', async () => {
    setToken('existing-token')
    vi.mocked(api.getCurrentUser).mockResolvedValue(approverUser)

    renderApp()

    expect(await screen.findByRole('navigation')).toBeInTheDocument()
    expect(screen.getByText('Ada Approver')).toBeInTheDocument()
  })

  it('falls back to login when a stored token is rejected', async () => {
    setToken('stale-token')
    vi.mocked(api.getCurrentUser).mockRejectedValue(
      new ApiError('Not authenticated.', 401),
    )

    renderApp()

    expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    expect(getToken()).toBeNull()
  })

  it('shows the signed-in identity and role', async () => {
    setToken('token')
    vi.mocked(api.getCurrentUser).mockResolvedValue(operatorUser)

    renderApp()

    expect(await screen.findByText('Ola Operator')).toBeInTheDocument()
    expect(screen.getByText('operator@agentguard.local')).toBeInTheDocument()
    expect(screen.getByText('OPERATOR')).toBeInTheDocument()
  })

  it('signs out and clears the token', async () => {
    setToken('token')
    vi.mocked(api.getCurrentUser).mockResolvedValue(operatorUser)

    renderApp()

    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Sign out' }))

    expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    expect(getToken()).toBeNull()
  })

  it('hides navigation a role cannot use', async () => {
    setToken('token')
    vi.mocked(api.getCurrentUser).mockResolvedValue(viewerUser)

    renderApp()

    const nav = await screen.findByRole('navigation')

    // A viewer may read everything but may not run the agent.
    expect(nav).toHaveTextContent('Runs')
    expect(nav).toHaveTextContent('Audit')
    expect(nav).not.toHaveTextContent('Agent')
  })

  it('shows the Agent link to a role that may run it', async () => {
    setToken('token')
    vi.mocked(api.getCurrentUser).mockResolvedValue(operatorUser)

    renderApp()

    expect(await screen.findByRole('navigation')).toHaveTextContent('Agent')
  })
})
