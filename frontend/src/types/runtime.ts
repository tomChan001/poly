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

export interface ExecutablePair {
  id: string
  title: string
  kalshi_market_id: string
  kalshi_outcome: 'yes' | 'no'
  kalshi_rule_text: string
  kalshi_rule_url: string
  polymarket_market_id: string
  polymarket_outcome: 'yes' | 'no'
  polymarket_rule_text: string
  polymarket_rule_url: string
  minimum_quantity: string
  quantity_step: string
  enabled: boolean
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
