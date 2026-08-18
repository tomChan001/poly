import type { Opportunity } from '../types/opportunity'


export function AnalyticsPage({ opportunities }: { opportunities: Opportunity[] }) {
  const exact = opportunities.filter((item) => item.mapping_status === 'exact').length
  const fresh = opportunities.filter((item) => item.book_age_ms <= 2000).length

  return (
    <div className="page">
      <section className="page-heading"><div><h2>运行指标</h2><p>影子执行与数据质量</p></div></section>
      <section className="metric-strip analytics">
        <div><span>EXACT 映射机会</span><strong>{exact}</strong><small>当前窗口</small></div>
        <div><span>行情新鲜率</span><strong>{opportunities.length ? ((fresh / opportunities.length) * 100).toFixed(1) : '0.0'}%</strong><small>阈值 2 秒</small></div>
        <div><span>影子双腿匹配率</span><strong>—</strong><small>等待样本</small></div>
        <div><span>资本日收益</span><strong>—</strong><small>等待结算</small></div>
      </section>
      <section className="data-band"><h3>执行时间线</h3><div className="empty-chart"><span>尚无影子执行记录</span></div></section>
    </div>
  )
}

