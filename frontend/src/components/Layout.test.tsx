import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { Layout } from './Layout'
import { renderWithRouter } from '../test/render'
import { adminUser } from '../test/factories'

describe('Layout navigation', () => {
  it('offers Inbox and Knowledge between Agent and Runs', () => {
    renderWithRouter(
      <Layout>
        <div />
      </Layout>,
    )

    const nav = screen.getByRole('navigation', { name: 'Primary' })
    const labels = Array.from(nav.querySelectorAll('a')).map((link) => link.textContent)

    expect(labels).toEqual([
      'Overview',
      'Agent',
      'Inbox',
      'Enquiries',
      'Vacancy',
      'Knowledge',
      'Runs',
      'Approvals',
      'Audit',
      'Tools',
    ])
  })

  it('links Inbox to its route', () => {
    renderWithRouter(
      <Layout>
        <div />
      </Layout>,
    )

    expect(screen.getByRole('link', { name: 'Inbox' })).toHaveAttribute(
      'href',
      '/inbox',
    )
  })

  it('links Knowledge to its route', () => {
    renderWithRouter(
      <Layout>
        <div />
      </Layout>,
    )

    expect(screen.getByRole('link', { name: 'Knowledge' })).toHaveAttribute(
      'href',
      '/knowledge',
    )
  })

  it('hides Inbox and Knowledge from a user who cannot view runs', () => {
    renderWithRouter(
      <Layout>
        <div />
      </Layout>,
      { user: { ...adminUser, permissions: ['VIEW_TOOLS'] } },
    )

    expect(screen.queryByRole('link', { name: 'Inbox' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Knowledge' })).not.toBeInTheDocument()
  })
})
