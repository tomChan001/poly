import type { Execution } from '../types/execution'
import type { Opportunity } from '../types/opportunity'


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
