import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'

import App from './App'

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
      return Promise.resolve({
        ok: true,
        json: async () => (url.includes('/api/executions') ? executions : opportunities),
      })
    }),
  )

  render(<App />)

  expect(
    await screen.findByRole('heading', { name: '跨市场控制台' }),
  ).toBeInTheDocument()
  expect(await screen.findByText('Example market')).toBeInTheDocument()
  expect(screen.getByText('READ ONLY')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: '运行' }))
  expect(await screen.findByText('PAIRED')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /批准|下单/ })).not.toBeInTheDocument()
})
