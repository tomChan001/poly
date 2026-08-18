import type { Execution } from '../types/execution'


export function ExecutionDetailPage({ execution }: { execution: Execution }) {
  return (
    <section className="data-band execution-detail" aria-label="执行证据">
      <div className="execution-head">
        <div>
          <h3>执行证据</h3>
          <code>{execution.correlation_id}</code>
        </div>
        <strong className={`execution-state ${execution.state}`}>
          {execution.state.toUpperCase()}
        </strong>
      </div>
      <dl className="execution-summary">
        <div><dt>请求数量</dt><dd>{execution.requested_quantity}</dd></div>
        <div><dt>配对数量</dt><dd>{execution.matched_quantity}</dd></div>
        <div><dt>未对冲数量</dt><dd>{execution.unhedged_quantity}</dd></div>
      </dl>
      <div className="execution-legs">
        {(['kalshi', 'polymarket'] as const).map((venue) => {
          const leg = execution.legs[venue]
          return (
            <div key={venue}>
              <span>{venue === 'kalshi' ? 'Kalshi' : 'Polymarket'}</span>
              <strong>{leg?.status.toUpperCase() ?? '未提交'}</strong>
              <small>{leg ? `${leg.filled_quantity} 份 · ${leg.client_order_id}` : '无订单证据'}</small>
            </div>
          )
        })}
      </div>
      <ol className="execution-timeline">
        {execution.transitions.map((transition) => (
          <li key={`${transition.target}-${transition.occurred_at}`}>
            <time>{new Date(transition.occurred_at).toLocaleString('zh-CN')}</time>
            <span>{transition.source.toUpperCase()} → {transition.target.toUpperCase()}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}
