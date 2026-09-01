export type TradingMode = 'read_only' | 'shadow' | 'limited_auto'

export interface SystemStatus {
  status: string
  trading_mode: TradingMode
  opening_enabled: boolean
  reason: string
}
