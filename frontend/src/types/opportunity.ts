export interface Opportunity {
  id: string
  event: string
  kalshi_outcome: string
  polymarket_outcome: string
  mapping_status: string
  quantity: string
  kalshi_vwap: string
  polymarket_vwap: string
  total_fees: string
  deployed_capital: string
  payout: string
  profit_floor: string
  conservative_roi: string
  expected_settlement_at: string
  worst_case_settlement_at: string
  book_age_ms: number
  rejection_reasons: string[]
  rule_versions?: [string, string]
  book_sequences?: [string, string]
  balance_versions?: [string, string]
  risk_policy_version?: string
  fee_status?: 'calculated' | 'unknown'
}
