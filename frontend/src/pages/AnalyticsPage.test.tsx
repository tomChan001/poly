import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'

import type { Opportunity } from '../types/opportunity'
import { AnalyticsPage } from './AnalyticsPage'

afterEach(() => {
  cleanup()
})

const unavailableAgeOpportunity: Opportunity = {
  id: 'opp-null-age',
  event: 'Unmeasured book',
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
  book_age_ms: null,
  rejection_reasons: [],
}

const freshOpportunity: Opportunity = {
  ...unavailableAgeOpportunity,
  id: 'opp-fresh',
  event: 'Fresh book',
  book_age_ms: 180,
}

test('does not count unavailable ages as fresh and keeps them in the denominator', () => {
  render(
    <AnalyticsPage
      opportunities={[unavailableAgeOpportunity, freshOpportunity]}
      executions={[]}
    />,
  )

  const metrics = screen.getByText('行情新鲜率').closest('div')

  expect(metrics).not.toBeNull()
  expect(metrics!).toHaveTextContent('50.0%')
})
