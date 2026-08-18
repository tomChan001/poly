export interface ExecutionLeg {
  client_order_id: string
  status: string
  filled_quantity: string
}

export interface ExecutionTransition {
  source: string
  target: string
  occurred_at: string
}

export interface Execution {
  correlation_id: string
  state: string
  requested_quantity: string
  matched_quantity: string
  unhedged_quantity: string
  legs: Partial<Record<'kalshi' | 'polymarket', ExecutionLeg>>
  transitions: ExecutionTransition[]
}
