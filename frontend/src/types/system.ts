export interface SystemStatus {
  status: string
  opening_enabled: boolean
  reason: string
}

export interface OpeningControlState {
  opening_enabled: boolean
  reason: string
  version: number
}
