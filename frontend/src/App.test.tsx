import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import App from './App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
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
  expect(screen.getByRole('textbox', { name: 'Oddpool API 地址' })).toBeInTheDocument()
  expect(screen.getByLabelText('Oddpool API Token')).toHaveAttribute('type', 'password')
  expect(screen.getByRole('textbox', { name: 'Kalshi API 地址' })).toHaveValue(
    'https://api.elections.kalshi.com',
  )
  expect(screen.getByLabelText('Kalshi 环境')).toHaveValue('production')
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
  expect(screen.queryByRole('heading', { name: '新建市场对' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存市场对' })).not.toBeInTheDocument()

  for (const checkbox of screen.getAllByRole('checkbox', { name: /^已核对/ })) {
    fireEvent.click(checkbox)
  }
  fireEvent.click(screen.getByRole('button', { name: '确认 EXACT' }))

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
