import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  DesktopBootScreen,
  desktopBootPropsFromLocation,
  type DesktopUiState,
} from './DesktopBootScreen'

afterEach(() => {
  cleanup()
  delete window.__POLY_DESKTOP__
})

describe('DesktopBootScreen', () => {
  test.each<[DesktopUiState, string]>([
    ['initializing', '正在准备 Poly'],
    ['preparing_database', '正在准备本地数据库'],
    ['migrating', '正在升级本地数据'],
    ['starting_services', '正在启动服务'],
    ['restarting', '服务中断，正在重启'],
    ['keychain_denied', '无法访问系统钥匙串'],
    ['migration_failed', '本地数据升级失败'],
    ['runtime_unavailable', '本地服务无法启动'],
    ['permission_denied', '没有足够的系统权限'],
    ['resource_missing', '桌面运行资源缺失'],
    ['protocol_failed', '本地服务通信失败'],
    ['shutting_down', '正在安全退出'],
  ])('renders the allowlisted %s copy', (state, heading) => {
    render(<DesktopBootScreen state={state} />)

    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    expect(screen.getAllByRole('heading')).toHaveLength(1)
    expect(screen.getByTestId('desktop-explanation')).not.toBeEmptyDOMElement()
  })

  test.each<DesktopUiState>([
    'initializing',
    'preparing_database',
    'migrating',
    'starting_services',
    'restarting',
    'shutting_down',
  ])('shows progress without retry while %s', (state) => {
    render(<DesktopBootScreen state={state} />)

    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })

  test.each<DesktopUiState>([
    'keychain_denied',
    'migration_failed',
    'runtime_unavailable',
    'permission_denied',
    'resource_missing',
    'protocol_failed',
  ])('shows retry without progress for %s', (state) => {
    render(<DesktopBootScreen state={state} />)

    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
  })

  test('routes actions through the narrow desktop adapter', () => {
    const retry = vi.fn()
    const revealLogs = vi.fn()
    window.__POLY_DESKTOP__ = { retry, revealLogs }
    render(<DesktopBootScreen state="migration_failed" canRevealLogs />)

    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    fireEvent.click(screen.getByRole('button', { name: '在 Finder 中显示诊断日志' }))

    expect(retry).toHaveBeenCalledOnce()
    expect(revealLogs).toHaveBeenCalledOnce()
  })

  test('omits the logs action unless explicitly enabled and tolerates a missing adapter', () => {
    render(<DesktopBootScreen state="runtime_unavailable" />)

    expect(screen.queryByRole('button', { name: '在 Finder 中显示诊断日志' })).not.toBeInTheDocument()
    expect(() => fireEvent.click(screen.getByRole('button', { name: '重试' }))).not.toThrow()
  })
})

describe('desktopBootPropsFromLocation', () => {
  test('accepts only an allowlisted state on the bundled scheme', () => {
    expect(desktopBootPropsFromLocation({
      protocol: 'tauri:',
      search: '?desktop-state=migrating&can-reveal-logs=true',
    })).toEqual({ state: 'migrating', canRevealLogs: true })
  })

  test.each([
    ['tauri:', '?desktop-state=%3Cscript%3Ealert(1)%3C%2Fscript%3E'],
    ['tauri:', '?desktop-state=ready'],
    ['https:', '?desktop-state=migrating'],
    ['http:', '?desktop-state=runtime_unavailable&can-reveal-logs=true'],
    ['tauri:', '?can-reveal-logs=true'],
  ])('falls back to the normal app for invalid launch input', (protocol, search) => {
    expect(desktopBootPropsFromLocation({ protocol, search })).toBeNull()
  })

  test('does not treat arbitrary log flag values as permission', () => {
    expect(desktopBootPropsFromLocation({
      protocol: 'tauri:',
      search: '?desktop-state=protocol_failed&can-reveal-logs=yes',
    })).toEqual({ state: 'protocol_failed', canRevealLogs: false })
  })
})
