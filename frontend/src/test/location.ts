import { screen } from '@testing-library/react'

/**
 * The router's current search string, e.g. "?status=FAILED".
 *
 * MemoryRouter never touches window.location, so assertions about the URL read
 * the probe rendered by renderWithRouter instead.
 */
export function locationSearch(): string {
  return screen.getByTestId('location-search').textContent ?? ''
}
