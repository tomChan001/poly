import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
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

test('renders the operational opportunity table', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: async () => opportunities }),
  )

  render(<App />)

  expect(
    await screen.findByRole('heading', { name: '跨市场控制台' }),
  ).toBeInTheDocument()
  expect(await screen.findByText('Example market')).toBeInTheDocument()
  expect(screen.getByText('READ ONLY')).toBeInTheDocument()
})
