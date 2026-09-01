import { useEffect, useMemo, useState } from 'react'

import type { Execution } from '../types/execution'
import { executionTimestamp, groupExecutionsByDay } from '../utils/executionHistory'


interface HistoryPageProps {
  executions: Execution[]
}

const quantityFormatter = new Intl.NumberFormat('zh-CN', {
  maximumFractionDigits: 4,
})

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})


function legStatus(execution: Execution, venue: 'kalshi' | 'polymarket'): string {
  return execution.legs[venue]?.status.toUpperCase() ?? '未提交'
}


export function HistoryPage({ executions }: HistoryPageProps) {
  const days = useMemo(() => groupExecutionsByDay(executions), [executions])
  const [selectedDate, setSelectedDate] = useState('')

  useEffect(() => {
    if (!days.some((day) => day.date === selectedDate)) {
      setSelectedDate(days[0]?.date ?? '')
    }
  }, [days, selectedDate])

  const selected = days.find((day) => day.date === selectedDate) ?? days[0]

  return (
    <div className="page history-page">
      <section className="page-heading history-heading">
        <div><h2>每日执行历史</h2><p>按北京时间汇总真实订单记录</p></div>
        <label className="history-date-control">
          <span>日期</span>
          <select
            aria-label="选择日期"
            value={selected?.date ?? ''}
            disabled={days.length === 0}
            onChange={(event) => setSelectedDate(event.target.value)}
          >
            {days.length === 0
              ? <option value="">暂无记录</option>
              : days.map((day) => (
                <option key={day.date} value={day.date}>{day.label}</option>
              ))}
          </select>
        </label>
      </section>

      {!selected
        ? <section className="empty-panel"><strong>暂无执行历史</strong><span>真实订单执行后会按天显示在这里</span></section>
        : <>
          <section className="metric-strip history-metrics" aria-label="当日统计">
            <div><span>执行次数</span><strong>{selected.total}</strong><small>{selected.label}</small></div>
            <div><span>双腿匹配</span><strong>{selected.paired}</strong><small>PAIRED</small></div>
            <div><span>匹配数量</span><strong>{quantityFormatter.format(selected.matchedQuantity)}</strong><small>两腿共同成交</small></div>
            <div><span>未对冲数量</span><strong className={selected.unhedgedQuantity > 0 ? 'warning' : ''}>{quantityFormatter.format(selected.unhedgedQuantity)}</strong><small>需关注敞口</small></div>
          </section>

          <section className="table-section history-table-section">
            <div className="section-toolbar">
              <div><h3>当日执行记录</h3><p>{selected.total} 条记录，时间均为北京时间</p></div>
            </div>
            <div className="table-wrap">
              <table className="history-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>关联 ID</th>
                    <th>结果</th>
                    <th>申请数量</th>
                    <th>匹配数量</th>
                    <th>未对冲</th>
                    <th>Kalshi</th>
                    <th>Polymarket</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.executions.map((execution) => {
                    const timestamp = executionTimestamp(execution)
                    return (
                      <tr key={execution.correlation_id}>
                        <td>{timestamp ? timeFormatter.format(timestamp) : '—'}</td>
                        <td><code>{execution.correlation_id}</code></td>
                        <td><span className={`execution-state ${execution.state}`}>{execution.state.toUpperCase()}</span></td>
                        <td>{execution.requested_quantity}</td>
                        <td>{execution.matched_quantity}</td>
                        <td className={Number(execution.unhedged_quantity) > 0 ? 'warning' : ''}>{execution.unhedged_quantity}</td>
                        <td>{legStatus(execution, 'kalshi')}</td>
                        <td>{legStatus(execution, 'polymarket')}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>}
    </div>
  )
}
