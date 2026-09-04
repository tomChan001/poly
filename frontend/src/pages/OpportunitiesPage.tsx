import { useMemo, useState } from 'react'
import { ChevronRight, CircleAlert, Search, X } from 'lucide-react'

import type { Opportunity } from '../types/opportunity'


interface OpportunitiesPageProps {
  opportunities: Opportunity[]
  activeCount: number
  loading: boolean
}


const unavailableValue = '—'
const isUnavailable = <T,>(value: T | null | undefined): value is null | undefined => value == null
const formatNumericString = (
  value: string | null | undefined,
  formatter: (numericValue: number) => string,
) => {
  if (isUnavailable(value)) {
    return unavailableValue
  }

  return formatter(Number(value))
}
const money = (value: string | null | undefined) => formatNumericString(value, (numericValue) => `$${numericValue.toFixed(2)}`)
const price = (value: string | null | undefined) => formatNumericString(value, (numericValue) => numericValue.toFixed(2))
const percent = (value: string | null | undefined) => formatNumericString(value, (numericValue) => `${(numericValue * 100).toFixed(2)}%`)
const quantity = (value: string | null | undefined) => formatNumericString(value, (numericValue) => numericValue.toFixed(0))
const age = (value: number | null | undefined) => (isUnavailable(value) ? unavailableValue : `${value} ms`)
const isStaleAge = (value: number | null | undefined) => !isUnavailable(value) && value > 2000
const rejectionLabels: Record<string, string> = {
  FEE_UNKNOWN: '手续费规则未知',
  BELOW_MINIMUM_QUANTITY: '可执行数量低于市场最小值',
  BOOK_FRESHNESS_EVIDENCE_MISSING: '缺少盘口新鲜度证据',
  BOOK_SEQUENCE_UNSAFE: '盘口序列不连续',
  EVENT_LIMIT: '事件资金上限已触发',
  INSUFFICIENT_DEPTH: '盘口深度不足',
  KALSHI_BALANCE_INSUFFICIENT: 'Kalshi 可用资金不足',
  MAPPING_NOT_EXACT: '市场映射未通过精确审核',
  POLYMARKET_BALANCE_INSUFFICIENT: 'Polymarket 可用资金不足',
  PORTFOLIO_LIMIT: '组合资金上限已触发',
  PER_TRADE_LIMIT: '单笔资金上限已触发',
  ROI_BELOW_THRESHOLD: '保守收益率低于门槛',
  SETTLEMENT_TOO_LATE: '最晚结算时间超出限制',
  STALE_BOOK: '盘口已过期',
}

const rejectionLabel = (reason: string) => rejectionLabels[reason] ?? reason


export function OpportunitiesPage({ opportunities, activeCount, loading }: OpportunitiesPageProps) {
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'all' | 'eligible' | 'rejected'>('all')
  const [selected, setSelected] = useState<Opportunity | null>(null)

  const filtered = useMemo(() => {
    return opportunities.filter((item) => {
      const matchesQuery = item.event.toLowerCase().includes(query.toLowerCase())
      const rejected = item.rejection_reasons.length > 0
      const matchesStatus = status === 'all' || (status === 'rejected' ? rejected : !rejected)
      return matchesQuery && matchesStatus
    })
  }, [opportunities, query, status])

  const roiValues = opportunities
    .map((item) => item.conservative_roi)
    .filter((value): value is string => value != null)
    .map((value) => Number(value))
  const bestRoi = roiValues.length > 0 ? Math.max(...roiValues) : null
  const totalCapital = opportunities.reduce(
    (sum, item) => sum + (item.deployed_capital == null ? 0 : Number(item.deployed_capital)),
    0,
  )

  return (
    <div className="page">
      <section className="metric-strip" aria-label="机会概览">
        <div><span>可执行机会</span><strong>{activeCount}</strong><small>当前快照</small></div>
        <div><span>最高保守 ROI</span><strong className={bestRoi == null ? '' : 'positive'}>{percent(bestRoi == null ? null : String(bestRoi))}</strong><small>扣除费用与缓冲</small></div>
        <div><span>预计占用资本</span><strong>${totalCapital.toFixed(2)}</strong><small>未提交</small></div>
        <div><span>待审核映射</span><strong className="warning">0</strong><small>需人工判断</small></div>
      </section>

      <section className="table-section">
        <div className="section-toolbar">
          <div>
            <h2>实时机会</h2>
            <p>按保守净 ROI 排序</p>
          </div>
          <div className="table-controls">
            <div className="segmented" aria-label="机会状态">
              {(['all', 'eligible', 'rejected'] as const).map((value) => (
                <button
                  type="button"
                  key={value}
                  className={status === value ? 'selected' : ''}
                  onClick={() => setStatus(value)}
                >
                  {value === 'all' ? '全部' : value === 'eligible' ? '可执行' : '已拒绝'}
                </button>
              ))}
            </div>
            <label className="search-box">
              <Search size={16} />
              <input
                aria-label="搜索事件"
                placeholder="搜索事件"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
          </div>
        </div>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>事件</th>
                <th>交易组合</th>
                <th>数量</th>
                <th>投入</th>
                <th>净利润下界</th>
                <th>保守 ROI</th>
                <th>行情年龄</th>
                <th aria-label="详情" />
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => {
                const rejected = item.rejection_reasons.length > 0
                return (
                  <tr key={item.id} onClick={() => setSelected(item)} tabIndex={0}>
                    <td>
                      <strong className="event-name">{item.event}</strong>
                      <span className={rejected ? 'status rejected' : 'status eligible'}>
                        {rejected ? `已拒绝 · ${rejectionLabel(item.rejection_reasons[0])}` : item.mapping_status.toUpperCase()}
                      </span>
                    </td>
                    <td>
                      <div className="venue-pair">
                        <span><i className="venue-dot kalshi" />K {item.kalshi_outcome} @{price(item.kalshi_vwap)}</span>
                        <span><i className="venue-dot poly" />P {item.polymarket_outcome} @{price(item.polymarket_vwap)}</span>
                      </div>
                    </td>
                    <td>{quantity(item.quantity)}</td>
                    <td>{money(item.deployed_capital)}</td>
                    <td className={rejected || item.profit_floor == null ? '' : 'positive'}>{money(item.profit_floor)}</td>
                    <td><strong className={rejected || item.conservative_roi == null ? '' : 'positive'}>{percent(item.conservative_roi)}</strong></td>
                    <td><span className={isStaleAge(item.book_age_ms) ? 'age stale' : 'age'}>{age(item.book_age_ms)}</span></td>
                    <td><ChevronRight size={17} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {!loading && filtered.length === 0 && (
            <div className="empty-state">
              <CircleAlert size={22} />
              <strong>当前没有符合条件的机会</strong>
              <span>扫描结果将在原生数据校验后显示</span>
            </div>
          )}
          {loading && <div className="loading-row">正在读取原生行情...</div>}
        </div>
      </section>

      {selected && (
        <aside className="detail-drawer" aria-label="机会详情">
          <div className="drawer-head">
            <div><span>机会详情</span><h2>{selected.event}</h2></div>
            <button type="button" className="icon-button" aria-label="关闭详情" onClick={() => setSelected(null)}><X size={18} /></button>
          </div>
          <dl className="detail-grid">
            <div><dt>映射状态</dt><dd>{selected.mapping_status.toUpperCase()}</dd></div>
            <div><dt>最优数量</dt><dd>{quantity(selected.quantity)}</dd></div>
            <div><dt>Kalshi VWAP</dt><dd>{money(selected.kalshi_vwap)}</dd></div>
            <div><dt>Polymarket VWAP</dt><dd>{money(selected.polymarket_vwap)}</dd></div>
            <div><dt>费用</dt><dd>{money(selected.total_fees)}</dd></div>
            <div><dt>费用状态</dt><dd>{selected.fee_status === 'unknown' || selected.rejection_reasons.includes('FEE_UNKNOWN') ? '未知（禁止执行）' : '已计算'}</dd></div>
            <div><dt>行情年龄</dt><dd>{age(selected.book_age_ms)}</dd></div>
            <div><dt>保守 ROI</dt><dd className={selected.conservative_roi == null ? '' : 'positive'}>{percent(selected.conservative_roi)}</dd></div>
          </dl>
          <div className="drawer-section">
            <h3>拒绝原因</h3>
            {selected.rejection_reasons.length === 0
              ? <p className="positive">全部硬风控已通过</p>
              : selected.rejection_reasons.map((reason) => <p key={reason} className="rejection-line">{rejectionLabel(reason)}</p>)}
          </div>
          <div className="drawer-section">
            <h3>结算窗口</h3>
            <p>{new Date(selected.expected_settlement_at).toLocaleDateString('zh-CN')} 至 {new Date(selected.worst_case_settlement_at).toLocaleDateString('zh-CN')}</p>
          </div>
        </aside>
      )}
    </div>
  )
}
