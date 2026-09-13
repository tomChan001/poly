export interface RuntimeStatus {
  ready: boolean
  running: boolean
  opening_enabled: boolean
  missing_providers: string[]
  last_cycle_at: string | null
  last_error: string | null
  executions_started: number
}

export type MappingStatus = 'pending_review' | 'exact' | 'conditional' | 'rejected' | 'stale'

export interface PairPreview {
  eligible: boolean
  rejection_reasons: string[]
  evaluated_at: string
  valid_until?: string | null
  risk_policy_version: string
  conservative_roi: string | null
  gross_roi: string | null
  quantity: string | null
  kalshi_best_ask: string | null
  polymarket_best_ask: string | null
  kalshi_best_ask_quantity: string | null
  polymarket_best_ask_quantity: string | null
  paired_liquidity: string | null
  kalshi_vwap: string | null
  polymarket_vwap: string | null
  total_fees: string | null
  deployed_capital: string | null
  profit_floor: string | null
}

export interface ExecutablePair {
  id: string
  title: string
  kalshi_market_id: string
  kalshi_outcome: 'yes' | 'no'
  kalshi_rule_text: string
  kalshi_rule_url: string
  kalshi_market_url?: string
  polymarket_market_id: string
  polymarket_outcome: 'yes' | 'no'
  polymarket_rule_text: string
  polymarket_rule_url: string
  polymarket_market_url?: string
  polymarket_resolution_source?: string
  preview?: PairPreview | null
  minimum_quantity: string | number
  quantity_step: string | number
  enabled: boolean
  kalshi_expected_settlement_at: string | null
  polymarket_expected_settlement_at: string | null
  worst_case_settlement_at: string | null
  kalshi_category: string
  polymarket_category: string
  kalshi_minimum_tick: string | number
  polymarket_minimum_tick: string | number
  native_fingerprint: string
  material_fingerprint: string
  status: MappingStatus
  checklist: Record<string, boolean> | null
  truth_table: Array<Record<string, string>>
  notes: string
  reviewed_by: string | null
  source_candidate_id: string | null
  source_updated_at: string | null
}

export interface PairReviewInput {
  status: 'exact' | 'conditional' | 'rejected'
  checklist: Record<string, boolean>
  truth_table: Array<{ kalshi: string; polymarket: string }>
  notes: string
}
