import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import App from './App'
import { OpportunitiesPage } from './pages/OpportunitiesPage'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

test('loads and saves the active risk policy', async () => {
  let savedBody: Record<string, unknown> | null = null
  const initialPolicy = riskPolicy
  const savedPolicy = {
    ...riskPolicy,
    version: 'risk-v2',
    created_at: '2026-09-03T03:30:00Z',
    minimum_roi: '0.05',
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') {
          savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
          return Promise.resolve({ ok: true, json: async () => savedPolicy })
        }
        return Promise.resolve({ ok: true, json: async () => initialPolicy })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/health')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))

  expect(await screen.findByLabelText('最低保守 ROI')).toHaveValue(3)
  expect(screen.getByLabelText('最长预计结算')).toHaveValue(30)
  expect(screen.getByLabelText('显式成本')).toHaveValue(0)
  expect(screen.getByLabelText('最大行情到达间隔')).toHaveValue(0.5)
  expect(screen.getByText('策略版本 risk-v1')).toBeInTheDocument()
  expect(screen.getByText(/创建于.*2026年9月3日/)).toBeInTheDocument()

  fireEvent.change(screen.getByLabelText('最低保守 ROI'), { target: { value: '5' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))

  await waitFor(() => expect(savedBody).not.toBeNull())
  expect(savedBody).toEqual({
    minimum_roi: '0.05',
    maximum_settlement_days: 30,
    maximum_book_age_seconds: '2',
    per_trade_limit: '10',
    per_event_limit: '25',
    portfolio_limit: '100',
    explicit_cost: '0',
    risk_buffer: '0.25',
    maximum_unhedged_seconds: '2',
    maximum_unhedged_loss: '2',
    maximum_arrival_gap_seconds: '0.5',
  })
  expect(await screen.findByText('已生成新策略版本')).toBeInTheDocument()
  expect(screen.getByText('策略版本 risk-v2')).toBeInTheDocument()
  expect(screen.getByLabelText('最低保守 ROI')).toHaveValue(5)
})

test('keeps risk edits visible when saving the policy fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') {
          return Promise.resolve({
            ok: false,
            status: 500,
            json: async () => ({ detail: 'database unavailable' }),
          })
        }
        return Promise.resolve({ ok: true, json: async () => riskPolicy })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/health')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  const roi = await screen.findByLabelText('最低保守 ROI')
  fireEvent.change(roi, { target: { value: '5.5' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))

  expect(await screen.findByText('保存失败：database unavailable')).toBeInTheDocument()
  expect(roi).toHaveValue(5.5)
})

test('loads and saves precise decimal risk limits without discrete step restrictions', async () => {
  let savedBody: Record<string, unknown> | null = null
  const precisePolicy = {
    ...riskPolicy,
    minimum_roi: '0.0125',
    maximum_book_age_seconds: '3.33',
    per_trade_limit: '1.25',
    per_event_limit: '2.75',
    explicit_cost: '0.25',
    risk_buffer: '0.333',
    maximum_unhedged_seconds: '0.25',
    maximum_unhedged_loss: '1.25',
    maximum_arrival_gap_seconds: '0.25',
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') {
          savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
        }
        return Promise.resolve({ ok: true, json: async () => precisePolicy })
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))

  expect(await screen.findByLabelText('最低保守 ROI')).toHaveValue(1.25)
  expect(screen.getByLabelText('行情最大年龄')).toHaveValue(3.33)
  expect(screen.getByLabelText('显式成本')).toHaveValue(0.25)
  expect(screen.getByLabelText('最低保守 ROI')).toHaveAttribute('step', 'any')
  expect(screen.getByLabelText('最大未对冲时长')).toHaveAttribute('step', 'any')
  expect(screen.getByLabelText('单笔上限')).toHaveAttribute('step', 'any')
  expect(screen.getByLabelText('最长预计结算')).toHaveAttribute('step', '1')

  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))
  await waitFor(() => expect(savedBody).toMatchObject({
    minimum_roi: '0.0125',
    maximum_book_age_seconds: '3.33',
    per_trade_limit: '1.25',
    explicit_cost: '0.25',
    maximum_arrival_gap_seconds: '0.25',
  }))
})

test('blocks an empty risk value before saving', async () => {
  let saveRequests = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') saveRequests += 1
        return Promise.resolve({ ok: true, json: async () => ({ ...riskPolicy, minimum_roi: '' }) })
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  await screen.findByLabelText('最低保守 ROI')
  await waitFor(() => expect(screen.getByRole('group')).not.toBeDisabled())
  expect(screen.getByLabelText('最低保守 ROI')).toHaveValue(null)
  const saveButton = screen.getByRole('button', { name: '保存策略' })
  expect(saveButton).toBeEnabled()
  fireEvent.click(saveButton)
  expect(await screen.findByText('最低保守 ROI 不能为空')).toBeInTheDocument()
  expect(saveRequests).toBe(0)
})

test('blocks an out-of-range risk value before saving', async () => {
  let saveRequests = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') saveRequests += 1
        return Promise.resolve({ ok: true, json: async () => riskPolicy })
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  const minimumRoi = await screen.findByLabelText('最低保守 ROI')

  fireEvent.change(minimumRoi, { target: { value: '101' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))
  expect(await screen.findByText('最低保守 ROI 必须在 0% 到 100% 之间')).toBeInTheDocument()
  expect(saveRequests).toBe(0)
})

test('rejects scientific notation in risk decimal fields before saving', async () => {
  let saveRequests = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') saveRequests += 1
        return Promise.resolve({ ok: true, json: async () => riskPolicy })
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  const minimumRoi = await screen.findByLabelText('最低保守 ROI')

  fireEvent.change(minimumRoi, { target: { value: '3e-1' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))
  expect(await screen.findByText('最低保守 ROI 必须使用普通十进制表示')).toBeInTheDocument()
  expect(saveRequests).toBe(0)

  fireEvent.change(minimumRoi, { target: { value: '5e0' } })
  fireEvent.click(screen.getByRole('button', { name: '保存策略' }))
  expect(await screen.findByText('最低保守 ROI 必须使用普通十进制表示')).toBeInTheDocument()
  expect(saveRequests).toBe(0)
})

test('aborts a pending risk-policy load when leaving the page', async () => {
  const riskRequest: { signal: AbortSignal | null } = { signal: null }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        riskRequest.signal = init?.signal ?? null
        return new Promise(() => undefined)
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  await waitFor(() => expect(riskRequest.signal).not.toBeNull())

  fireEvent.click(screen.getByRole('button', { name: '机会' }))
  expect(riskRequest.signal?.aborted).toBe(true)
})

test('retries a transient risk policy load failure', async () => {
  let riskRequests = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        riskRequests += 1
        if (riskRequests === 1) {
          return Promise.resolve({ ok: false, status: 503, json: async () => ({ detail: '服务暂不可用' }) })
        }
        return Promise.resolve({ ok: true, json: async () => riskPolicy })
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  expect(await screen.findByText('加载失败：服务暂不可用')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findByLabelText('最低保守 ROI')).toHaveValue(3)
  expect(riskRequests).toBe(2)
})

test('shows readable FastAPI validation details when saving fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/settings/risk')) {
        if (init?.method === 'PUT') {
          return Promise.resolve({
            ok: false,
            status: 422,
            json: async () => ({ detail: [{ loc: ['body', 'minimum_roi'], msg: 'Input should be less than or equal to 1' }] }),
          })
        }
        return Promise.resolve({ ok: true, json: async () => riskPolicy })
      }
      if (url.includes('/api/runtime')) return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      if (url.includes('/health')) return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '风控' }))
  fireEvent.click(await screen.findByRole('button', { name: '保存策略' }))

  expect(await screen.findByText('保存失败：minimum_roi：Input should be less than or equal to 1')).toBeInTheDocument()
})

test('opens the integration menu with write-only credential inputs', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      let payload: unknown = opportunities
      if (url.includes('/api/executions')) payload = executions
      if (url.includes('/api/integrations')) payload = []
      if (url.includes('/api/runtime')) payload = runtimeStatus
      if (url.includes('/api/pairs')) payload = []
      if (url.includes('/health')) {
        payload = {
          status: 'ok',
          trading_mode: 'limited_auto',
          opening_enabled: true,
          reason: 'configured default',
        }
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      return Promise.resolve({ ok: true, json: async () => payload })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '集成' }))

  expect(await screen.findByRole('heading', { name: '集成配置' })).toBeInTheDocument()
  expect(screen.getByText('https://api.oddpool.com')).toBeInTheDocument()
  expect(screen.queryByRole('textbox', { name: 'Oddpool API 地址' })).not.toBeInTheDocument()
  expect(screen.getByLabelText('Oddpool API Token')).toHaveAttribute('type', 'password')
  expect(screen.getByRole('textbox', { name: 'Kalshi API 地址' })).toHaveValue(
    'https://api.elections.kalshi.com',
  )
  expect(screen.getByLabelText('Kalshi 环境')).toHaveValue('production')
  expect(
    screen.getByRole('link', { name: 'Kalshi 官方 API Key 获取说明' }),
  ).toHaveAttribute('href', 'https://docs.kalshi.com/getting_started/api_keys')
  expect(screen.getByText(/Account & security → API Keys/)).toBeInTheDocument()
  expect(screen.getByText(/私钥只显示和下载一次/)).toBeInTheDocument()
  expect(
    screen.getByText(/API Key ID 填入 Key ID；下载的 .key 文件完整内容填入 RSA 私钥/),
  ).toBeInTheDocument()
  expect(screen.getByLabelText('RSA 私钥')).toHaveAttribute('type', 'password')
  expect(screen.getByLabelText('Polymarket 账户类型')).toHaveValue('magic_proxy')
  expect(screen.getByText('Google / Magic 登录不需要密码，也不会在这里收集密码。')).toBeInTheDocument()
  expect(
    screen.getByRole('link', { name: '官方导出私钥说明' }),
  ).toHaveAttribute(
    'href',
    'https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key',
  )
  expect(screen.getByLabelText('钱包私钥（仅写入）')).toHaveAttribute('type', 'password')
  expect(screen.getByLabelText('派生 Owner 地址')).toHaveAttribute('readonly')
  expect(screen.getByLabelText('派生 Proxy 地址')).toHaveAttribute('readonly')
  expect(screen.getByLabelText('派生 Funder 地址')).toHaveAttribute('readonly')
  expect(screen.getByLabelText('派生签名类型')).toHaveAttribute('readonly')
  fireEvent.change(screen.getByLabelText('Polymarket 账户类型'), {
    target: { value: 'gnosis_safe' },
  })
  expect(screen.getByLabelText('高级 Funder 地址')).not.toHaveAttribute('readonly')
  expect(screen.getByLabelText('派生 Funder 地址')).toHaveAttribute('readonly')
  expect(
    screen.getByRole('button', { name: '测试连接（不会下单）' }),
  ).toBeDisabled()
  expect(screen.queryByRole('heading', { name: 'OIDC' })).not.toBeInTheDocument()
})

test('always saves the canonical Oddpool API address', async () => {
  let savedBody: Record<string, unknown> | null = null
  const legacyOddpoolIntegration = {
    provider: 'oddpool',
    enabled: true,
    environment: 'production',
    base_url: 'https://legacy.example.test',
    configuration: {},
    version: 1,
    updated_at: '2026-08-26T00:00:00Z',
    updated_by: 'operator-1',
    secret_status: {
      api_token: { configured: false, fingerprint: null },
    },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/integrations/oddpool') && init?.method === 'PUT') {
        savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ...legacyOddpoolIntegration,
            base_url: 'https://api.oddpool.com',
            version: 2,
          }),
        })
      }
      if (url.endsWith('/api/integrations')) {
        return Promise.resolve({ ok: true, json: async () => [legacyOddpoolIntegration] })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/health')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '集成' }))
  expect(await screen.findByText('https://api.oddpool.com')).toBeInTheDocument()
  expect(screen.queryByText('https://legacy.example.test')).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Oddpool API Token'), {
    target: { value: 'secret-token' },
  })
  const panel = screen.getByRole('heading', { name: 'Oddpool' }).closest('form')
  expect(panel).not.toBeNull()
  fireEvent.click(within(panel!).getByRole('button', { name: '保存' }))

  await waitFor(() => expect(savedBody).not.toBeNull())
  expect(savedBody).toMatchObject({
    base_url: 'https://api.oddpool.com',
    secrets: { api_token: 'secret-token' },
  })
})

test('does not resubmit stale Magic addresses after changing account type', async () => {
  let savedBody: Record<string, unknown> | null = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/integrations/polymarket') && init?.method === 'PUT') {
        savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({ ok: true, json: async () => polymarketIntegration })
      }
      if (url.endsWith('/api/integrations')) {
        return Promise.resolve({ ok: true, json: async () => [polymarketIntegration] })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/health')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '集成' }))
  fireEvent.change(await screen.findByLabelText('Polymarket 账户类型'), {
    target: { value: 'eoa' },
  })
  const panel = screen.getByRole('heading', { name: 'Polymarket' }).closest('form')
  expect(panel).not.toBeNull()
  fireEvent.click(within(panel!).getByRole('button', { name: '保存' }))

  await waitFor(() => expect(savedBody).not.toBeNull())
  const submitted = savedBody as unknown as Record<string, unknown>
  const configuration = submitted.configuration as Record<string, unknown>
  expect(configuration.account_type).toBe('eoa')
  expect(configuration).not.toHaveProperty('owner_address')
  expect(configuration).not.toHaveProperty('proxy_address')
  expect(configuration).not.toHaveProperty('funder_address')
})

test('clearing an advanced funder removes the stale saved value', async () => {
  let savedBody: Record<string, unknown> | null = null
  const safeIntegration = {
    ...polymarketIntegration,
    configuration: {
      ...polymarketIntegration.configuration,
      account_type: 'gnosis_safe',
      signature_type: 2,
    },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/integrations/polymarket') && init?.method === 'PUT') {
        savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return Promise.resolve({ ok: true, json: async () => safeIntegration })
      }
      if (url.endsWith('/api/integrations')) {
        return Promise.resolve({ ok: true, json: async () => [safeIntegration] })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/health')) {
        return Promise.resolve({ ok: true, json: async () => ({ status: 'ok' }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '集成' }))
  const funder = await screen.findByLabelText('高级 Funder 地址')
  expect(funder).toHaveValue(polymarketIntegration.configuration.funder_address)
  fireEvent.change(funder, { target: { value: '' } })
  const panel = screen.getByRole('heading', { name: 'Polymarket' }).closest('form')
  expect(panel).not.toBeNull()
  fireEvent.click(within(panel!).getByRole('button', { name: '保存' }))

  await waitFor(() => expect(savedBody).not.toBeNull())
  const submitted = savedBody as unknown as Record<string, unknown>
  const configuration = submitted.configuration as Record<string, unknown>
  expect(configuration).not.toHaveProperty('funder_address')
})

const polymarketIntegration = {
  provider: 'polymarket',
  enabled: true,
  environment: 'production',
  base_url: 'https://clob.polymarket.com',
  configuration: {
    account_type: 'magic_proxy',
    owner_address: '0x1111111111111111111111111111111111111111',
    proxy_address: '0x2222222222222222222222222222222222222222',
    funder_address: '0x2222222222222222222222222222222222222222',
    signature_type: 1,
    chain_id: 137,
  },
  version: 1,
  updated_at: '2026-08-26T00:00:00Z',
  updated_by: 'operator-1',
  secret_status: {
    private_key: { configured: true, fingerprint: 'sha256:123456789abc' },
  },
}

const riskPolicy = {
  version: 'risk-v1',
  created_at: '2026-09-03T02:00:00Z',
  minimum_roi: '0.03',
  maximum_settlement_days: 30,
  maximum_book_age_seconds: '2',
  per_trade_limit: '10',
  per_event_limit: '25',
  portfolio_limit: '100',
  explicit_cost: '0',
  risk_buffer: '0.25',
  maximum_unhedged_seconds: '2',
  maximum_unhedged_loss: '2',
  maximum_arrival_gap_seconds: '0.5',
}

const opportunities = [
  {
    id: 'opp-1',
    event: 'Example market',
    kalshi_outcome: 'NO',
    polymarket_outcome: 'YES',
    mapping_status: 'exact',
    quantity: '10',
    kalshi_vwap: '0.70',
    polymarket_vwap: '0.20',
    total_fees: '0.02',
    deployed_capital: '9.02',
    payout: '10',
    profit_floor: '0.98',
    conservative_roi: '0.1086',
    expected_settlement_at: '2026-09-01T00:00:00Z',
    worst_case_settlement_at: '2026-09-08T00:00:00Z',
    book_age_ms: 180,
    rejection_reasons: [],
  },
]

const executions = [
  {
    correlation_id: 'corr-1',
    state: 'paired',
    requested_quantity: '10',
    matched_quantity: '10',
    unhedged_quantity: '0',
    legs: {
      kalshi: { client_order_id: 'corr-1-kalshi', status: 'filled', filled_quantity: '10' },
      polymarket: { client_order_id: 'corr-1-polymarket', status: 'filled', filled_quantity: '10' },
    },
    transitions: [
      { source: 'submitted', target: 'paired', occurred_at: '2026-08-18T04:00:00Z' },
    ],
  },
]

test('renders the operational opportunity table', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/health')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            trading_mode: 'limited_auto',
            opening_enabled: true,
            reason: 'configured default',
          }),
        })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      return Promise.resolve({
        ok: true,
        json: async () => url.includes('/api/executions')
          ? executions
          : url.includes('/api/pairs') ? [] : opportunities,
      })
    }),
  )

  render(<App />)

  expect(
    await screen.findByRole('heading', { name: '跨市场控制台' }),
  ).toBeInTheDocument()
  expect(await screen.findByText('Example market')).toBeInTheDocument()
  expect(screen.getByText('LIMITED AUTO')).toBeInTheDocument()
  expect(screen.getByText('真实订单已启用')).toBeInTheDocument()
  expect(screen.getByText('ON')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '运行' }))
  expect(await screen.findByText('PAIRED')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /批准|下单/ })).not.toBeInTheDocument()
})

test('explains structured opportunity rejection evidence in Chinese', () => {
  const rejected = {
    ...opportunities[0],
    id: 'rejected-1',
    book_age_ms: 2501,
    rejection_reasons: ['STALE_BOOK', 'FEE_UNKNOWN'],
  }

  render(
    <OpportunitiesPage
      opportunities={[rejected]}
      activeCount={0}
      loading={false}
    />,
  )
  fireEvent.click(screen.getByText('Example market'))

  expect(screen.getByText('盘口已过期')).toBeInTheDocument()
  expect(screen.getByText('手续费规则未知')).toBeInTheDocument()
  expect(screen.getByText('未知（禁止执行）')).toBeInTheDocument()
  expect(screen.getAllByText('2501 ms')).toHaveLength(2)
})

test('groups execution history by Beijing calendar day', async () => {
  const historyExecutions = [
    {
      ...executions[0],
      correlation_id: 'corr-latest',
      transitions: [
        { source: 'submitted', target: 'paired', occurred_at: '2026-08-18T16:30:00Z' },
      ],
    },
    {
      ...executions[0],
      correlation_id: 'corr-previous',
      state: 'partially_hedged',
      matched_quantity: '8',
      unhedged_quantity: '2',
      transitions: [
        {
          source: 'submitted',
          target: 'partially_hedged',
          occurred_at: '2026-08-17T08:00:00Z',
        },
      ],
    },
  ]

  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/health')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            trading_mode: 'limited_auto',
            opening_enabled: true,
            reason: 'configured default',
          }),
        })
      }
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      return Promise.resolve({
        ok: true,
        json: async () => url.includes('/api/executions')
          ? historyExecutions
          : url.includes('/api/pairs') ? [] : opportunities,
      })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '历史' }))

  expect(screen.getByRole('heading', { name: '每日执行历史' })).toBeInTheDocument()
  expect(screen.getByLabelText('选择日期')).toHaveValue('2026-08-19')
  expect(screen.getByText('corr-latest')).toBeInTheDocument()
  expect(screen.queryByText('corr-previous')).not.toBeInTheDocument()

  fireEvent.change(screen.getByLabelText('选择日期'), {
    target: { value: '2026-08-17' },
  })

  expect(screen.getByText('corr-previous')).toBeInTheDocument()
  expect(screen.queryByText('corr-latest')).not.toBeInTheDocument()
})

const runtimeStatus = {
  ready: true,
  running: true,
  opening_enabled: true,
  missing_providers: [],
  last_cycle_at: '2026-08-19T02:00:00Z',
  last_error: null,
  executions_started: 3,
}

const pendingPair = {
  id: 'pair-1',
  title: '示例互补市场',
  kalshi_market_id: 'K-MARKET',
  kalshi_outcome: 'no',
  kalshi_rule_text: 'Kalshi 完整规则',
  kalshi_rule_url: 'https://kalshi.test/rule',
  polymarket_market_id: 'P-TOKEN',
  polymarket_outcome: 'yes',
  polymarket_rule_text: 'Polymarket 完整规则',
  polymarket_rule_url: 'https://poly.test/rule',
  minimum_quantity: '10',
  quantity_step: '1',
  enabled: true,
  status: 'pending_review',
  checklist: null,
  truth_table: [],
  notes: '',
  reviewed_by: null,
  source_candidate_id: 'oddpool-browser',
  source_updated_at: '2026-08-21T02:00:00Z',
}

test('shows real runtime state and lets a human review market equivalence', async () => {
  let reviewBody: Record<string, unknown> | null = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.includes('/api/pairs/pair-1/review')) {
        reviewBody = JSON.parse(String(init?.body)) as Record<string, unknown>
        return Promise.resolve({
          ok: true,
          json: async () => ({ ...pendingPair, status: 'exact', checklist: reviewBody?.checklist }),
        })
      }
      if (url.endsWith('/api/pairs')) {
        return Promise.resolve({ ok: true, json: async () => [pendingPair] })
      }
      if (url.includes('/health')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            trading_mode: 'limited_auto',
            opening_enabled: true,
            reason: 'configured default',
          }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: async () => (url.includes('/api/executions') ? [] : opportunities),
      })
    }),
  )

  render(<App />)
  expect(await screen.findByText('自动执行运行中')).toBeInTheDocument()
  expect(screen.getByText('已启动 3 次执行')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '审核' }))
  expect(await screen.findByRole('heading', { name: '市场对审核' })).toBeInTheDocument()
  expect(await screen.findByRole('heading', { name: '示例互补市场' })).toBeInTheDocument()
  const candidateScrollRegion = screen.getByRole('region', { name: 'Oddpool 候选内容' })
  const candidateHeading = screen.getByRole('heading', { name: 'Oddpool 候选' })
  const candidateButton = await screen.findByRole('button', { name: /示例互补市场/ })
  expect(candidateScrollRegion).toHaveClass('pair-list-scroll')
  expect(candidateScrollRegion).toHaveAttribute('tabindex', '0')
  expect(within(candidateScrollRegion).getByRole('button', { name: /示例互补市场/ })).toBe(candidateButton)
  expect(candidateScrollRegion).toContainElement(candidateButton)
  expect(candidateScrollRegion).not.toContainElement(candidateHeading)
  expect(screen.queryByRole('heading', { name: '新建市场对' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存市场对' })).not.toBeInTheDocument()

  for (const checkbox of screen.getAllByRole('checkbox', { name: /^已核对/ })) {
    fireEvent.click(checkbox)
  }
  expect(
    screen.getAllByRole('checkbox', { name: /^已核对/ })
      .filter((checkbox) => !(checkbox as HTMLInputElement).checked)
      .map((checkbox) => checkbox.getAttribute('aria-label')),
  ).toEqual([])
  const confirmExact = screen.getByRole('button', { name: '确认 EXACT' })
  expect(confirmExact).toBeEnabled()
  fireEvent.click(confirmExact)

  expect(await screen.findByText('审核已保存，可进入自动执行')).toBeInTheDocument()
  expect(reviewBody).toMatchObject({
    status: 'exact',
    truth_table: [
      { kalshi: '1', polymarket: '0' },
      { kalshi: '0', polymarket: '1' },
    ],
  })
  expect(screen.queryByRole('button', { name: /批准订单|下单/ })).not.toBeInTheDocument()
})

test('preserves an unsaved review draft across automatic candidate refreshes', async () => {
  vi.useFakeTimers()
  try {
    let pairReads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/api/runtime')) {
          return Promise.resolve({ ok: true, json: async () => runtimeStatus })
        }
        if (url.endsWith('/api/pairs')) {
          pairReads += 1
          return Promise.resolve({ ok: true, json: async () => [{ ...pendingPair }] })
        }
        if (url.includes('/health')) {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              status: 'ok',
              trading_mode: 'limited_auto',
              opening_enabled: true,
              reason: 'configured default',
            }),
          })
        }
        return Promise.resolve({
          ok: true,
          json: async () => (url.includes('/api/executions') ? [] : opportunities),
        })
      }),
    )

    render(<App />)
    await act(async () => { await Promise.resolve() })
    fireEvent.click(screen.getByRole('button', { name: '审核' }))
    await act(async () => { await Promise.resolve() })

    const subject = screen.getByRole('checkbox', { name: '已核对标的主体' })
    const notes = screen.getByRole('textbox', { name: '审核备注' })
    fireEvent.click(subject)
    fireEvent.change(notes, { target: { value: '尚未提交的审核备注' } })

    await act(async () => {
      vi.advanceTimersByTime(5_000)
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(pairReads).toBe(2)
    expect(subject).toBeChecked()
    expect(notes).toHaveValue('尚未提交的审核备注')
  } finally {
    vi.useRealTimers()
  }
})

test('discards the current draft without prompting when switching candidates', async () => {
  const confirm = vi.fn()
  const secondPair = {
    ...pendingPair,
    id: 'pair-2',
    title: '第二个互补市场',
    checklist: { subject: true },
    notes: '第二个候选的已保存备注',
  }
  vi.stubGlobal('confirm', confirm)
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.endsWith('/api/pairs')) {
        return Promise.resolve({ ok: true, json: async () => [pendingPair, secondPair] })
      }
      if (url.includes('/health')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            trading_mode: 'limited_auto',
            opening_enabled: true,
            reason: 'configured default',
          }),
        })
      }
      return Promise.resolve({
        ok: true,
        json: async () => (url.includes('/api/executions') ? [] : opportunities),
      })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '审核' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: '已核对标的主体' }))
  fireEvent.change(screen.getByRole('textbox', { name: '审核备注' }), {
    target: { value: '第一个候选的未保存备注' },
  })
  fireEvent.click(screen.getByRole('button', { name: /第二个互补市场/ }))

  expect(screen.getByRole('checkbox', { name: '已核对标的主体' })).toBeChecked()
  expect(screen.getByRole('textbox', { name: '审核备注' })).toHaveValue('第二个候选的已保存备注')
  expect(confirm).not.toHaveBeenCalled()
})

test('keeps the empty oddpool candidate state inside the scroll region', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/runtime')) {
        return Promise.resolve({ ok: true, json: async () => runtimeStatus })
      }
      if (url.endsWith('/api/pairs')) {
        return Promise.resolve({ ok: true, json: async () => [] })
      }
      if (url.includes('/health')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            trading_mode: 'limited_auto',
            opening_enabled: true,
            reason: 'configured default',
          }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }),
  )

  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: '审核' }))

  const candidateScrollRegion = await screen.findByRole('region', { name: 'Oddpool 候选内容' })
  const candidateHeading = screen.getByRole('heading', { name: 'Oddpool 候选' })

  expect(within(candidateScrollRegion).getByText('暂无自动发现的待审核候选')).toBeInTheDocument()
  expect(candidateScrollRegion).not.toContainElement(candidateHeading)
})
