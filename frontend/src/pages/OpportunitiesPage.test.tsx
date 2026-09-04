import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'

import type { Opportunity } from '../types/opportunity'
import { OpportunitiesPage } from './OpportunitiesPage'

afterEach(() => {
  cleanup()
})

const lateSettlementOpportunity: Opportunity = {
  id: 'late-settlement',
  event: 'Late settlement pair',
  kalshi_outcome: 'no',
  polymarket_outcome: 'yes',
  mapping_status: 'exact',
  quantity: null,
  kalshi_vwap: null,
  polymarket_vwap: null,
  total_fees: null,
  deployed_capital: null,
  payout: null,
  profit_floor: null,
  conservative_roi: null,
  expected_settlement_at: '2026-09-01T00:00:00Z',
  worst_case_settlement_at: '2026-09-08T00:00:00Z',
  book_age_ms: null,
  rejection_reasons: ['SETTLEMENT_TOO_LATE'],
  fee_status: 'unknown',
}

const staleBookOpportunity: Opportunity = {
  ...lateSettlementOpportunity,
  id: 'stale-book',
  event: 'Stale book pair',
  book_age_ms: 2501,
  rejection_reasons: ['STALE_BOOK'],
}

const calculatedOpportunity: Opportunity = {
  id: 'accepted-calculated',
  event: 'Calculated pair',
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
  fee_status: 'calculated',
}

const zeroOpportunity: Opportunity = {
  ...calculatedOpportunity,
  id: 'genuine-zero',
  event: 'Zero pair',
  quantity: '0',
  kalshi_vwap: '0',
  polymarket_vwap: '0',
  total_fees: '0',
  deployed_capital: '0',
  payout: '0',
  profit_floor: '0',
  conservative_roi: '0',
  book_age_ms: 0,
}

function renderPage(opportunities: Opportunity[], activeCount = 0) {
  render(
    <OpportunitiesPage
      opportunities={opportunities}
      activeCount={activeCount}
      loading={false}
    />,
  )
}

test('renders unavailable metrics as dashes and shows the first localized rejection reason', () => {
  renderPage([lateSettlementOpportunity])

  const row = screen.getByText('Late settlement pair').closest('tr')

  expect(row).not.toBeNull()
  expect(row!).toHaveTextContent('已拒绝 · 最晚结算时间超出限制')
  expect(row!).toHaveTextContent('K no @—')
  expect(row!).toHaveTextContent('P yes @—')
  expect(within(row!).getAllByText('—')).toHaveLength(5)

  const overview = screen.getByLabelText('机会概览')
  const highestRoiMetric = within(overview).getByText('最高保守 ROI').closest('div')

  expect(within(highestRoiMetric!).getByText('—')).toBeInTheDocument()

  fireEvent.click(screen.getByText('Late settlement pair'))

  const drawer = screen.getByLabelText('机会详情')

  expect(within(drawer).getAllByText('—')).toHaveLength(6)
  expect(within(drawer).getByText('最晚结算时间超出限制')).toBeInTheDocument()
})

test('keeps measured stale-book age visible while unavailable quote metrics stay dashed', () => {
  renderPage([staleBookOpportunity])

  const row = screen.getByText('Stale book pair').closest('tr')
  const age = within(row!).getByText('2501 ms')

  expect(age).toHaveClass('age')
  expect(age).toHaveClass('stale')
  expect(row!).toHaveTextContent('K no @—')
  expect(row!).toHaveTextContent('P yes @—')
})

test('renders calculated opportunity metrics with numeric formatting', () => {
  renderPage([calculatedOpportunity], 1)

  const row = screen.getByText('Calculated pair').closest('tr')

  expect(within(row!).getByText('EXACT')).toBeInTheDocument()
  expect(within(row!).getByText('K NO @0.70')).toBeInTheDocument()
  expect(within(row!).getByText('P YES @0.20')).toBeInTheDocument()
  expect(within(row!).getByText('10')).toBeInTheDocument()
  expect(within(row!).getByText('$9.02')).toBeInTheDocument()
  expect(within(row!).getByText('$0.98')).toBeInTheDocument()
  expect(within(row!).getByText('10.86%')).toBeInTheDocument()
  expect(within(row!).getByText('180 ms')).toBeInTheDocument()
})

test('formats genuine zero values numerically instead of showing dashes', () => {
  renderPage([zeroOpportunity], 1)

  const row = screen.getByText('Zero pair').closest('tr')

  expect(within(row!).getByText('K NO @0.00')).toBeInTheDocument()
  expect(within(row!).getByText('P YES @0.00')).toBeInTheDocument()
  expect(within(row!).getByText('0')).toBeInTheDocument()
  expect(within(row!).getAllByText('$0.00')).toHaveLength(2)
  expect(within(row!).getByText('0.00%')).toBeInTheDocument()
  expect(within(row!).getByText('0 ms')).toBeInTheDocument()
  expect(row!).not.toHaveTextContent('—')
})
