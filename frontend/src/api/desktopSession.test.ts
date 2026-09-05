import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  clearDesktopSessionForTest,
  installDesktopSessionFromLocation,
  sessionFetch,
} from './desktopSession'

afterEach(() => {
  clearDesktopSessionForTest()
  vi.unstubAllGlobals()
})

describe('desktop session capability', () => {
  it('moves the fragment capability into an explicit request header', async () => {
    const token = 'x'.repeat(43)
    const replaceState = vi.fn()
    const storage = new Map<string, string>()
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    installDesktopSessionFromLocation(
      { hash: `#poly_session=${token}`, pathname: '/', search: '' } as Location,
      { replaceState } as unknown as History,
      {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
      } as Storage,
    )
    await sessionFetch('/health')

    expect(replaceState).toHaveBeenCalledWith(null, '', '/')
    expect(storage.get('poly.desktop.session')).toBe(token)
    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.get('X-Poly-Desktop-Session')).toBe(token)
  })

  it('restores the capability from origin-scoped session storage after reload', async () => {
    const token = 'y'.repeat(43)
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    installDesktopSessionFromLocation(
      { hash: '', pathname: '/', search: '' } as Location,
      { replaceState: vi.fn() } as unknown as History,
      {
        getItem: () => token,
        setItem: vi.fn(),
      } as unknown as Storage,
    )
    await sessionFetch('/health')

    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.get('X-Poly-Desktop-Session')).toBe(token)
  })

  it('does not install malformed fragments', async () => {
    const replaceState = vi.fn()
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    installDesktopSessionFromLocation(
      { hash: '#poly_session=short', pathname: '/', search: '' } as Location,
      { replaceState } as unknown as History,
      {
        getItem: () => null,
        setItem: vi.fn(),
      } as unknown as Storage,
    )
    await sessionFetch('/health')

    expect(replaceState).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledWith('/health', undefined)
  })
})
