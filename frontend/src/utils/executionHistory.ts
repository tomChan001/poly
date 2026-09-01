import type { Execution } from '../types/execution'


export interface ExecutionHistoryDay {
  date: string
  label: string
  executions: Execution[]
  total: number
  paired: number
  matchedQuantity: number
  unhedgedQuantity: number
}

const dateLabelFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: 'long',
  day: 'numeric',
})

const dayPartsFormatter = new Intl.DateTimeFormat('en', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})


export function executionTimestamp(execution: Execution): Date | null {
  const timestamps = execution.transitions
    .map((transition) => new Date(transition.occurred_at))
    .filter((value) => !Number.isNaN(value.getTime()))

  if (timestamps.length === 0) return null
  return new Date(Math.min(...timestamps.map((value) => value.getTime())))
}


function beijingDayKey(value: Date): string {
  const parts = Object.fromEntries(
    dayPartsFormatter.formatToParts(value).map((part) => [part.type, part.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}`
}


function numeric(value: string): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}


export function groupExecutionsByDay(executions: Execution[]): ExecutionHistoryDay[] {
  const grouped = new Map<string, { label: string; executions: Execution[] }>()

  for (const execution of executions) {
    const timestamp = executionTimestamp(execution)
    const date = timestamp ? beijingDayKey(timestamp) : 'undated'
    const label = timestamp ? dateLabelFormatter.format(timestamp) : '日期未知'
    const bucket = grouped.get(date) ?? { label, executions: [] }
    bucket.executions.push(execution)
    grouped.set(date, bucket)
  }

  return [...grouped.entries()]
    .sort(([left], [right]) => {
      if (left === 'undated') return 1
      if (right === 'undated') return -1
      return right.localeCompare(left)
    })
    .map(([date, bucket]) => {
      const sorted = [...bucket.executions].sort((left, right) => {
        const leftTime = executionTimestamp(left)?.getTime() ?? 0
        const rightTime = executionTimestamp(right)?.getTime() ?? 0
        return rightTime - leftTime
      })
      return {
        date,
        label: bucket.label,
        executions: sorted,
        total: sorted.length,
        paired: sorted.filter((execution) => execution.state === 'paired').length,
        matchedQuantity: sorted.reduce(
          (total, execution) => total + numeric(execution.matched_quantity),
          0,
        ),
        unhedgedQuantity: sorted.reduce(
          (total, execution) => total + numeric(execution.unhedged_quantity),
          0,
        ),
      }
    })
}
