import type { Opportunity } from '../types/opportunity'


export async function getOpportunities(): Promise<Opportunity[]> {
  const response = await fetch('/api/opportunities')
  if (!response.ok) {
    throw new Error(`opportunity request failed: ${response.status}`)
  }
  return response.json() as Promise<Opportunity[]>
}

