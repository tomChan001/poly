import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import { IntegrationSettingsPage } from './IntegrationSettingsPage'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderPage() {
  return render(<IntegrationSettingsPage
    realOrderingEnabled={false}
    onRealOrderingChange={async () => {}}
    realOrderingPending={false}
    realOrderingError={null}
  />)
}

test('explains Mac credential re-entry without asking for a system password', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => [] })))
  renderPage()
  await waitFor(() => expect(document.querySelector('[aria-busy="true"]')).toBeNull())
  expect(screen.getByText(/Mac 桌面版不会请求系统密码/)).toBeInTheDocument()
  expect(screen.getByText(/安装新版后.*重新填写平台 API 密钥.*旧钥匙串记录会保留/)).toBeInTheDocument()
})

test('reports a failed credential save in Chinese and keeps the unsaved input', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => (
    init?.method === 'PUT'
      ? { ok: false, status: 503, json: async () => ({ detail: 'credential storage unavailable' }) }
      : { ok: true, json: async () => [] }
  )))
  renderPage()
  await waitFor(() => expect(document.querySelector('[aria-busy="true"]')).toBeNull())
  const input = screen.getByLabelText('Oddpool API Token')
  fireEvent.change(input, { target: { value: 'synthetic-unsaved-test-value' } })
  fireEvent.click(screen.getAllByRole('button', { name: /^保存$/ })[0])
  expect(await screen.findByText(/无法安全访问密钥存储/)).toBeInTheDocument()
  expect(input).toHaveValue('synthetic-unsaved-test-value')
  expect(screen.queryByText(/已保存版本/)).not.toBeInTheDocument()
  expect(screen.queryByText('credential storage unavailable')).not.toBeInTheDocument()
})

test('reports an unavailable credential store on load without a password instruction', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: false, status: 503, json: async () => ({ detail: 'credential storage unavailable' }),
  })))
  renderPage()
  expect(await screen.findAllByText(/无法安全访问密钥存储/)).toHaveLength(3)
  expect(screen.queryByText('credential storage unavailable')).not.toBeInTheDocument()
  expect(screen.getByRole('switch', { name: '真实下单' })).not.toBeChecked()
})
