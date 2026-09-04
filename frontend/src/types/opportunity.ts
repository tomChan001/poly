export interface Opportunity {
  id: string
  event: string
  kalshi_outcome: string
  polymarket_outcome: string
  mapping_status: string
  quantity: string | null
  kalshi_vwap: string | null
  polymarket_vwap: string | null
  total_fees: string | null
  deployed_capital: string | null
  payout: string | null
  profit_floor: string | null
  conservative_roi: string | null
  expected_settlement_at: string
  worst_case_settlement_at: string
  book_age_ms: number | null
  rejection_reasons: string[]
  rule_versions?: [string, string]
  book_sequences?: [string, string]
  balance_versions?: [string, string]
  risk_policy_version?: string
  fee_status?: 'calculated' | 'unknown'
}
