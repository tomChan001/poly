export interface RiskPolicy {
  version: string
  created_at: string
  minimum_roi: string
  maximum_settlement_days: number
  maximum_book_age_seconds: string
  per_trade_limit: string
  per_event_limit: string
  portfolio_limit: string
  explicit_cost: string
  risk_buffer: string
  maximum_unhedged_seconds: string
  maximum_unhedged_loss: string
  maximum_arrival_gap_seconds: string
}

export type RiskPolicyUpdate = Omit<RiskPolicy, 'version' | 'created_at'>
