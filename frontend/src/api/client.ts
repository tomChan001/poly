import type { Execution } from '../types/execution'
import type {
  ConnectionTestResult,
  IntegrationConfig,
  IntegrationProvider,
  IntegrationUpdate,
  SecretStatus,
} from '../types/integration'
import type { Opportunity } from '../types/opportunity'
import type { SystemStatus } from '../types/system'
import type { RiskPolicy, RiskPolicyUpdate } from '../types/risk'
import type {
  ExecutablePair,
  PairReviewInput,
  RuntimeStatus,
} from '../types/runtime'


export async function getOpportunities(): Promise<Opportunity[]> {
  const response = await fetch('/api/opportunities')
  if (!response.ok) {
    throw new Error(`opportunity request failed: ${response.status}`)
  }
  return response.json() as Promise<Opportunity[]>
}


export async function getExecutions(): Promise<Execution[]> {
  const response = await fetch('/api/executions')
  if (!response.ok) {
    throw new Error(`execution request failed: ${response.status}`)
  }
  return response.json() as Promise<Execution[]>
}


export async function getSystemStatus(): Promise<SystemStatus> {
  const response = await fetch('/health')
  if (!response.ok) {
    throw new Error(`system status request failed: ${response.status}`)
  }
  return response.json() as Promise<SystemStatus>
}


async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = formatApiDetail(payload?.detail) ?? `HTTP ${response.status}`
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}


function formatApiDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (!Array.isArray(detail)) return null

  const messages = detail.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const value = item as { loc?: unknown; msg?: unknown }
    if (typeof value.msg !== 'string') return []
    const location = Array.isArray(value.loc)
      ? value.loc.filter((part): part is string | number => typeof part === 'string' || typeof part === 'number')
        .filter((part) => part !== 'body')
        .join('.')
      : ''
    return [location ? `${location}：${value.msg}` : value.msg]
  })
  return messages.length > 0 ? messages.join('；') : null
}


export function getRuntimeStatus(): Promise<RuntimeStatus> {
  return apiRequest<RuntimeStatus>('/api/runtime')
}


export function getRiskPolicy(signal?: AbortSignal): Promise<RiskPolicy> {
  return apiRequest<RiskPolicy>('/api/settings/risk', { signal })
}


export function saveRiskPolicy(value: RiskPolicyUpdate): Promise<RiskPolicy> {
  return apiRequest<RiskPolicy>('/api/settings/risk', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(value),
  })
}


export function getPairs(): Promise<ExecutablePair[]> {
  return apiRequest<ExecutablePair[]>('/api/pairs')
}


export function reviewPair(pairId: string, value: PairReviewInput): Promise<ExecutablePair> {
  return apiRequest<ExecutablePair>(`/api/pairs/${pairId}/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(value),
  })
}


async function integrationRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/integrations${path}`, init)
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = typeof payload?.detail === 'string' ? payload.detail : `HTTP ${response.status}`
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}


export function getIntegrations(): Promise<IntegrationConfig[]> {
  return integrationRequest<IntegrationConfig[]>('')
}


export function saveIntegration(
  provider: IntegrationProvider,
  value: IntegrationUpdate,
): Promise<IntegrationConfig> {
  return integrationRequest<IntegrationConfig>(`/${provider}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(value),
  })
}


export function testIntegration(provider: IntegrationProvider): Promise<ConnectionTestResult> {
  return integrationRequest<ConnectionTestResult>(`/${provider}/test`, { method: 'POST' })
}


export function deleteIntegrationSecret(
  provider: IntegrationProvider,
  name: string,
): Promise<SecretStatus> {
  return integrationRequest<SecretStatus>(`/${provider}/secrets/${name}`, { method: 'DELETE' })
}
