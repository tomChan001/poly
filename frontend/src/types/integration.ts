export type IntegrationProvider = 'oddpool' | 'kalshi' | 'polymarket'
export type IntegrationEnvironment = 'sandbox' | 'production'

export interface SecretStatus {
  configured: boolean
  fingerprint: string | null
}

export interface IntegrationConfig {
  provider: IntegrationProvider
  enabled: boolean
  environment: IntegrationEnvironment
  base_url: string
  configuration: Record<string, string | number | boolean>
  version: number
  updated_at: string
  updated_by: string
  secret_status: Record<string, SecretStatus>
}

export interface IntegrationUpdate {
  enabled: boolean
  environment: IntegrationEnvironment
  base_url: string
  configuration: Record<string, string | number | boolean>
  secrets: Record<string, string>
}

export interface ConnectionTestResult {
  provider: IntegrationProvider
  ok: boolean
  code: string
  detail: string
  checked_at: string
}
