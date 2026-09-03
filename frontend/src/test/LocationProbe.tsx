import { useLocation } from 'react-router-dom'

/**
 * Exposes the router's location to assertions.
 *
 * MemoryRouter never touches window.location, so tests read the URL from this
 * hidden node via `locationSearch()`.
 */
export function LocationProbe() {
  const location = useLocation()

  return (
    <div data-testid="location-search" hidden>
      {location.search}
    </div>
  )
}
