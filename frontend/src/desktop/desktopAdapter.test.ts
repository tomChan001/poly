import { beforeEach, describe, expect, test, vi } from 'vitest'

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }))

vi.mock('@tauri-apps/api/core', () => ({ invoke }))

import { installDesktopAdapter } from './desktopAdapter'

describe('installDesktopAdapter', () => {
  beforeEach(() => {
    invoke.mockReset()
    delete window.__POLY_DESKTOP__
  })

  test('wires retry and log actions to fixed Tauri command names', async () => {
    invoke.mockResolvedValue(undefined)

    installDesktopAdapter()
    await window.__POLY_DESKTOP__?.retry()
    await window.__POLY_DESKTOP__?.revealLogs()

    expect(invoke).toHaveBeenNthCalledWith(1, 'retry_desktop_runtime')
    expect(invoke).toHaveBeenNthCalledWith(2, 'reveal_desktop_logs')
    expect(invoke.mock.calls.every((call) => call.length === 1)).toBe(true)
  })
})
