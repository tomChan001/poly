const CAPABILITY_HEADER = 'X-Poly-Desktop-Session'
const CAPABILITY_STORAGE_KEY = 'poly.desktop.session'
let capability: string | null = null

function validCapability(value: string | null): value is string {
  return value !== null && /^[A-Za-z0-9_-]{43,}$/.test(value)
}

export function installDesktopSessionFromLocation(
  location: Location,
  history: History,
  storage: Storage = window.sessionStorage,
): void {
  const parameters = new URLSearchParams(location.hash.startsWith('#') ? location.hash.slice(1) : '')
  const supplied = parameters.get('poly_session')
  if (validCapability(supplied)) {
    capability = supplied
    storage.setItem(CAPABILITY_STORAGE_KEY, supplied)
    history.replaceState(null, '', `${location.pathname}${location.search}`)
    return
  }

  const persisted = storage.getItem(CAPABILITY_STORAGE_KEY)
  capability = validCapability(persisted) ? persisted : null
}

export function sessionFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (!capability) return fetch(input, init)
  const headers = new Headers(init?.headers)
  headers.set(CAPABILITY_HEADER, capability)
  return fetch(input, { ...init, headers })
}

export function clearDesktopSessionForTest(): void {
  capability = null
}
