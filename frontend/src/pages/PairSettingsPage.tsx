import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { invoke, isTauri } from '@tauri-apps/api/core'
import { Check, RefreshCw, X } from 'lucide-react'

import { getPairs, reviewPair } from '../api/client'
import type { ExecutablePair, MappingStatus } from '../types/runtime'

const beijingDateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

const statusLabels: Record<MappingStatus, string> = {
  pending_review: '待审核',
  exact: '已确认配对',
  conditional: '有条件匹配',
  rejected: '不适合配对',
  stale: '需要重新审核',
}

function formatBeijingTime(value: string | null | undefined): string {
  if (!value) return '平台暂未提供'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '时间格式有误' : beijingDateFormatter.format(parsed)
}

function optionalText(value: string | null | undefined): string {
  return value?.trim() || '平台暂未提供'
}

function priceStep(value: string | number | null | undefined): string {
  return Number(value) > 0 ? `$${value}` : '平台暂未提供'
}

function safePlatformUrl(value: string | undefined, platform: 'kalshi' | 'polymarket'): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    const domain = `${platform}.com`
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password
      && (url.hostname === domain || url.hostname.endsWith(`.${domain}`)) ? url.href : null
  } catch { return null }
}

function metric(value: string | null | undefined, unit = ''): string {
  return value == null || value.trim() === '' ? '暂无证据' : unit === '$' ? `$${value}` : `${value}${unit}`
}

function roi(value: string | null | undefined): string {
  if (value == null || value.trim() === '' || !Number.isFinite(Number(value))) return '暂无证据'
  return `${Number((Number(value) * 100).toPrecision(10))}%`
}

function previewExpired(pair: ExecutablePair, now: number): boolean {
  const validUntil = pair.preview?.valid_until
  return validUntil != null && !(Date.parse(validUntil) > now)
}

function isQualified(pair: ExecutablePair, now: number): boolean {
  return pair.preview?.eligible === true && !previewExpired(pair, now)
}

function materialEvidence(pair: ExecutablePair): string {
  return pair.material_fingerprint || JSON.stringify([
    pair.kalshi_rule_text, pair.kalshi_rule_url, pair.polymarket_rule_text,
    pair.polymarket_rule_url, pair.polymarket_resolution_source,
  ])
}

const rejectionLabels: Record<string, string> = {
  PREVIEW_PENDING: '本轮测算尚未完成，正在轮换重试',
  PAIR_DISABLED: '候选未启用', MAPPING_REJECTED: '规则配对已被拒绝',
  SETTLEMENT_UNKNOWN: '结算时间证据不完整', SETTLEMENT_PASSED: '预计结算时间已过', SETTLEMENT_TOO_LATE: '预计结算超过风控时限',
  INSUFFICIENT_LIQUIDITY: '当前最优卖价的配对流动性不足', INSUFFICIENT_DEPTH: '可成交深度不足', BELOW_MINIMUM_QUANTITY: '可买数量低于最小下单数量',
  STALE_BOOK: '行情已过期', BOOK_FRESHNESS_EVIDENCE_MISSING: '缺少行情新鲜度证据', BOOK_TIME_IN_FUTURE: '行情时间异常', BOOK_ARRIVAL_GAP: '两边行情到达间隔过大',
  NATIVE_DATA_UNAVAILABLE: '平台原生行情暂不可用', FEE_UNKNOWN: '交易费用尚未确认', ROI_BELOW_THRESHOLD: '保守净 ROI 低于设定门槛',
  PER_TRADE_LIMIT: '超过单笔资本上限', EVENT_LIMIT: '超过单事件资本上限', PORTFOLIO_LIMIT: '超过总未结算资本上限', RISK_POLICY_UNAVAILABLE: '风控策略暂不可用',
  RISK_POLICY_CHANGED: '风控设置已更新，正在重新筛选',
}

const reviewItems = [
  {
    key: 'subject',
    auditLabel: '标的主体',
    label: '是不是在说同一件事？',
    description: '两个市场必须都在问同一个人、地点或事件。',
  },
  {
    key: 'threshold_boundary',
    auditLabel: '阈值边界',
    label: '判断标准一样吗？',
    description: '例如截止时间、数量或温度等具体标准必须相同。',
  },
  {
    key: 'timezone',
    auditLabel: '时区',
    label: '截止时间按哪个时区？',
    description: '同一天在不同地区可能不是同一个时刻。',
  },
  {
    key: 'occurrence_definition',
    auditLabel: '发生定义',
    label: '什么情况算“发生”？',
    description: '两边对事件成立的定义不能不同。',
  },
  {
    key: 'data_source',
    auditLabel: '数据来源',
    label: '以谁公布的结果为准？',
    description: '结果来源不同，最后的结算结果也可能不同。',
  },
  {
    key: 'postponement',
    auditLabel: '延期规则',
    label: '活动延期怎么办？',
    description: '确认两边遇到延期时会不会按同样方式处理。',
  },
  {
    key: 'cancellation',
    auditLabel: '取消规则',
    label: '活动取消怎么办？',
    description: '确认两边取消后是退款、作废还是继续等待。',
  },
  {
    key: 'invalid_result',
    auditLabel: '无效结果',
    label: '没有有效结果怎么办？',
    description: '例如结果无法确认时，两边的处理方式要一致。',
  },
  {
    key: 'dispute_process',
    auditLabel: '争议流程',
    label: '结果有争议怎么办？',
    description: '确认争议期间和最终裁定的规则不会冲突。',
  },
  {
    key: 'payout_unit',
    auditLabel: '结算单位',
    label: '结算方式能对应吗？',
    description: '确保一个市场赢、另一个市场输时，金额计算能够配对。',
  },
] as const

export function PairSettingsPage() {
  const [pairs, setPairs] = useState<ExecutablePair[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState('qualified')
  const [now, setNow] = useState(Date.now)
  const [checklist, setChecklist] = useState<Record<string, boolean>>({})
  const [notes, setNotes] = useState('')
  const draftPairId = useRef<string | null>(null)
  const draftMaterial = useRef<string | null>(null)
  const draftDirty = useRef(false)
  const loadRevision = useRef(0)
  const loadController = useRef<AbortController | null>(null)
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const visiblePairs = useMemo(
    () => pairs.filter((pair) => filter === 'all' || (filter === 'qualified' ? isQualified(pair, now) : !isQualified(pair, now))),
    [pairs, filter, now],
  )
  const selected = useMemo(
    () => pairs.find((pair) => pair.id === selectedId) ?? visiblePairs[0] ?? null,
    [pairs, visiblePairs, selectedId],
  )
  const qualifiedCount = pairs.filter((pair) => isQualified(pair, now)).length
  const preview = selected?.preview
  const pendingCount = pairs.filter((pair) => pair.status === 'pending_review').length
  const uncheckedCount = reviewItems.filter((item) => checklist[item.key] !== true).length

  const loadPairs = useCallback(async (showLoading = false, automatic = false) => {
    if (automatic && loadController.current) return
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    const revision = ++loadRevision.current
    if (showLoading) setLoading(true)
    try {
      const values = await getPairs(controller.signal)
      if (revision !== loadRevision.current) return
      setPairs(values)
      setError(null)
    } catch (value) {
      if (revision !== loadRevision.current) return
      setError(value instanceof Error ? value.message : '读取自动候选失败')
    } finally {
      if (loadController.current === controller) loadController.current = null
      if (revision === loadRevision.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadPairs(true)
    const timer = window.setInterval(() => void loadPairs(false, true), 5_000)
    return () => {
      window.clearInterval(timer)
      loadRevision.current += 1
      loadController.current?.abort()
      loadController.current = null
    }
  }, [loadPairs])

  useEffect(() => {
    const deadlines = pairs.map((pair) => Date.parse(pair.preview?.valid_until ?? ''))
      .filter((deadline) => Number.isFinite(deadline) && deadline > now)
    if (deadlines.length === 0) return
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(0, Math.min(...deadlines) - Date.now()))
    return () => window.clearTimeout(timer)
  }, [pairs, now])

  useEffect(() => {
    const currentTime = Date.now()
    const deadlines = pairs.map((pair) => Date.parse(pair.preview?.valid_until ?? ''))
      .filter((deadline) => Number.isFinite(deadline) && deadline > currentTime)
    if (deadlines.length === 0) return
    const timer = window.setTimeout(
      () => void loadPairs(false, true),
      Math.max(100, (Math.min(...deadlines) - currentTime) / 2),
    )
    return () => window.clearTimeout(timer)
  }, [pairs, loadPairs])

  useEffect(() => {
    if (!selected) return
    setSelectedId(selected.id)
    const evidence = materialEvidence(selected)
    if (selected.id === draftPairId.current && evidence === draftMaterial.current && draftDirty.current) return
    const materialChanged = selected.id === draftPairId.current && evidence !== draftMaterial.current
    setChecklist(materialChanged ? {} : selected.checklist ?? {})
    setNotes(selected.notes)
    draftPairId.current = selected.id
    draftMaterial.current = evidence
    draftDirty.current = materialChanged
    if (materialChanged) setMessage('规则内容已更新，请重新逐项核对。')
  }, [selected])

  const submitReview = async (status: 'exact' | 'rejected') => {
    if (!selected) return
    setPending(true)
    setError(null)
    try {
      const saved = await reviewPair(selected.id, {
        status,
        checklist: Object.fromEntries(
          reviewItems.map((item) => [item.key, checklist[item.key] === true]),
        ),
        truth_table: status === 'exact'
          ? [
              { kalshi: '1', polymarket: '0' },
              { kalshi: '0', polymarket: '1' },
            ]
          : [],
        notes,
      })
      loadRevision.current += 1
      setPairs((current) => current.map((pair) => pair.id === saved.id ? { ...saved, preview: saved.preview ?? pair.preview } : pair))
      if (draftPairId.current === saved.id) {
        setChecklist(saved.checklist ?? {})
        setNotes(saved.notes)
        draftDirty.current = false
      }
      void loadPairs()
      setMessage(
        status === 'exact'
          ? '已保存：系统会把这两个市场当作一对来评估。'
          : '候选已拒绝',
      )
    } catch (value) {
      setError(value instanceof Error ? value.message : '审核保存失败')
    } finally {
      setPending(false)
    }
  }

  const openPlatformLink = (event: MouseEvent<HTMLAnchorElement>) => {
    if (!isTauri()) return
    event.preventDefault()
    void invoke('open_market_url', { url: event.currentTarget.href }).catch(() => {
      setError('无法打开系统浏览器，请稍后重试。')
    })
  }

  return (
    <div className="page pair-page">
      <section className="page-heading">
        <div><h2>市场对审核</h2><p>Oddpool 自动发现，人工只确认规则等价性</p></div>
        <div className="pair-heading-actions">
          <span className="count-badge">{pendingCount} 待审核</span>
          <button
            type="button"
            className="icon-button"
            aria-label="刷新自动候选"
            title="刷新自动候选"
            onClick={() => void loadPairs(true)}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </section>

      {(error || message) && (
        <div className={error ? 'pair-message error' : 'pair-message success'}>
          {error ?? message}
        </div>
      )}

      <div className="pair-filters">
        <label>候选筛选<select aria-label="候选筛选" value={filter} onChange={(event) => { setSelectedId(null); setFilter(event.target.value) }}>
          <option value="qualified">只看符合条件</option>
          <option value="unqualified">未通过或缺少证据</option>
          <option value="all">全部候选</option>
        </select></label>
        <span>符合条件 {qualifiedCount} · 未通过或缺少证据 {pairs.length - qualifiedCount} · 全部 {pairs.length}</span>
      </div>

      <div className="pair-workspace">
        <section className="pair-list" aria-label="自动候选列表">
          <header><h3>Oddpool 候选</h3><span>{visiblePairs.length}</span></header>
          <div className="pair-list-scroll" role="region" aria-label="Oddpool 候选内容" tabIndex={0}>
            {!loading && pairs.length === 0 && (
              <div className="pair-empty">暂无自动发现的待审核候选</div>
            )}
            {!loading && pairs.length > 0 && visiblePairs.length === 0 && <div className="pair-empty">{filter === 'qualified' ? '当前没有符合条件的候选；可切换筛选查看未通过原因。' : '当前筛选下没有候选。'}</div>}
            {visiblePairs.map((pair) => (
              <button
                key={pair.id}
                type="button"
                className={pair.id === selected?.id ? 'pair-row selected' : 'pair-row'}
                onClick={() => { setSelectedId(pair.id); setMessage(null) }}
              >
                <strong>{pair.title}</strong>
                <span>{pair.kalshi_market_id} / {pair.polymarket_market_id}</span>
                <time dateTime={pair.worst_case_settlement_at ?? undefined}>
                  预计最晚结算：{formatBeijingTime(pair.worst_case_settlement_at)}
                </time>
                <span>净 ROI {roi(pair.preview?.conservative_roi)} · 流动性 {metric(pair.preview?.paired_liquidity, ' 份')}</span>
                <b className={`pair-status ${pair.status}`}>{statusLabels[pair.status]}</b>
              </button>
            ))}
          </div>
        </section>

        <section className="review-panel">
          {!selected && <div className="pair-empty">{pairs.length ? '请切换筛选查看候选及评估证据' : '等待 Oddpool 自动发现候选'}</div>}
          {selected && (
            <>
              <header className="review-head">
                <div>
                  <h3>{selected.title}</h3>
                  <span>
                    Kalshi 买 {selected.kalshi_outcome.toUpperCase()} · Polymarket 买 {selected.polymarket_outcome.toUpperCase()}
                    {selected.source_candidate_id ? ` · ${selected.source_candidate_id}` : ''}
                  </span>
                </div>
                <b className={`pair-status ${selected.status}`}>{statusLabels[selected.status]}</b>
              </header>
              <section className="pair-metadata-section pair-preview" aria-label="当前交易预览">
                <header><h4>当前交易预览</h4><span>{isQualified(selected, now) ? '通过当前风控预筛选' : '未通过或缺少证据'}</span></header>
                {!preview && <p>缺少评估证据，暂不能判定符合条件。</p>}
                {previewExpired(selected, now) && <p>行情已过期，请等待重新评估。</p>}
                {preview && <>
                  <p className="preview-context">评估于 {formatBeijingTime(preview.evaluated_at)}（北京时间） · 策略 <span>{preview.risk_policy_version || '暂无证据'}</span>。只读测算基于配置金额上限；实际下单还需检查余额及持仓。未审核时暂按两个结果互补测算，仍须人工确认规则等价，行情变化后需重新评估。</p>
                  {preview.rejection_reasons.length > 0 && <ul className="preview-reasons">{preview.rejection_reasons.map((reason) => <li key={reason}>{rejectionLabels[reason] ?? `未通过条件：${reason}`}</li>)}</ul>}
                  {preview.total_fees == null && <p>费用未确认，净 ROI 暂不可用。</p>}
                </>}
                <dl className="pair-metadata-grid preview-grid">
                  <div><dt>保守净 ROI（扣费与风险缓冲）</dt><dd>{preview?.total_fees == null ? '暂无证据' : roi(preview.conservative_roi)}</dd></div>
                  <div><dt>毛 ROI（未扣费用）</dt><dd>{roi(preview?.gross_roi)}</dd></div>
                  <div><dt>最优卖价配对流动性</dt><dd>{metric(preview?.paired_liquidity, ' 份')}</dd></div>
                  <div><dt>两边预计买入数量</dt><dd>{metric(preview?.quantity, ' 份')}</dd></div>
                  <div><dt>预估手续费</dt><dd>{metric(preview?.total_fees, '$')}</dd></div>
                  <div><dt>预计占用资本</dt><dd>{metric(preview?.deployed_capital, '$')}</dd></div>
                  <div><dt>保守利润下限</dt><dd>{metric(preview?.profit_floor, '$')}</dd></div>
                </dl>
                <div className="trade-leg-grid">{(['kalshi', 'polymarket'] as const).map((platform) => <article key={platform}>
                  <h4>{platform === 'kalshi' ? 'Kalshi' : 'Polymarket'} 买 {selected[`${platform}_outcome`].toUpperCase()}</h4>
                  <dl className="pair-metadata-grid trade-leg-metrics">
                    <div><dt>当前最优卖价</dt><dd>{metric(preview?.[`${platform}_best_ask`], '$')}</dd></div>
                    <div><dt>该价格可买数量</dt><dd>{metric(preview?.[`${platform}_best_ask_quantity`], ' 份')}</dd></div>
                    <div><dt>预计成交均价</dt><dd>{metric(preview?.[`${platform}_vwap`], '$')}</dd></div>
                  </dl>
                </article>)}</div>
                <p className="preview-context">配对流动性取两边当前最优卖价可买数量的较小值；实际成交均价按预计数量和盘口深度计算。</p>
              </section>
              <section className="pair-metadata-section" aria-labelledby="pair-time-title">
                <header><h4 id="pair-time-title">关键时间</h4><span>均为北京时间</span></header>
                <dl className="pair-metadata-grid time-grid">
                  <div><dt>候选更新时间</dt><dd>{formatBeijingTime(selected.source_updated_at)}</dd></div>
                  <div><dt>Kalshi 预计结算</dt><dd>{formatBeijingTime(selected.kalshi_expected_settlement_at)}</dd></div>
                  <div><dt>Polymarket 预计结算</dt><dd>{formatBeijingTime(selected.polymarket_expected_settlement_at)}</dd></div>
                  <div><dt>预计最晚结算</dt><dd>{formatBeijingTime(selected.worst_case_settlement_at)}</dd></div>
                </dl>
                <p className="preview-context">结算时间为平台预计，争议或延期可能延后。</p>
              </section>
              <section className="pair-metadata-section" aria-labelledby="pair-market-title">
                <header><h4 id="pair-market-title">市场信息</h4></header>
                <dl className="pair-metadata-grid">
                  <div><dt>来源编号</dt><dd>{selected.source_candidate_id ?? '平台暂未提供'}</dd></div>
                  <div><dt>当前状态</dt><dd>{statusLabels[selected.status]}</dd></div>
                  <div><dt>是否启用</dt><dd>{selected.enabled ? '已启用' : '未启用'}</dd></div>
                  <div><dt>审核人</dt><dd>{selected.reviewed_by ?? '尚未审核'}</dd></div>
                  <div><dt>最小下单数量</dt><dd>{selected.minimum_quantity} 份</dd></div>
                  <div><dt>数量递增单位</dt><dd>{selected.quantity_step} 份</dd></div>
                  <div><dt>Kalshi 市场类别</dt><dd>{optionalText(selected.kalshi_category)}</dd></div>
                  <div><dt>Polymarket 市场类别</dt><dd>{optionalText(selected.polymarket_category)}</dd></div>
                  <div><dt>Kalshi 最小价格变化</dt><dd>{priceStep(selected.kalshi_minimum_tick)}</dd></div>
                  <div><dt>Polymarket 最小价格变化</dt><dd>{priceStep(selected.polymarket_minimum_tick)}</dd></div>
                </dl>
              </section>
              <div className="rule-comparison">
                {(['kalshi', 'polymarket'] as const).map((platform) => {
                  const name = platform === 'kalshi' ? 'Kalshi' : 'Polymarket'
                  const ruleUrl = safePlatformUrl(selected[`${platform}_rule_url`], platform)
                  const marketUrl = safePlatformUrl(selected[`${platform}_market_url`], platform) ?? (platform === 'polymarket' ? ruleUrl : null)
                  return <article key={platform}>
                    <strong>{name}</strong>
                    <span>{selected[`${platform}_market_id`]}</span>
                    <div className="market-link-actions">
                      {marketUrl ? <a className="secondary-button" href={marketUrl} target="_blank" rel="noopener noreferrer" onClick={openPlatformLink}>打开 {name} 市场</a> : <span>暂无可用的官方市场链接</span>}
                      {ruleUrl && <a href={ruleUrl} target="_blank" rel="noopener noreferrer" onClick={openPlatformLink}>查看 {name} 规则来源</a>}
                    </div>
                    {platform === 'polymarket' && <p>结算依据：{optionalText(selected.polymarket_resolution_source)}</p>}
                    <p>{selected[`${platform}_rule_text`] || '平台暂未提供完整规则'}</p>
                  </article>
                })}
              </div>
              <section className="review-introduction" aria-labelledby="review-purpose-title">
                <h4 id="review-purpose-title">这一步在做什么？</h4>
                <p>请确认两个平台的规则说的是同一件事。只有结果能够一赢一输，它们才适合配成一对。</p>
                <p><strong>为什么必须全部核对？</strong>任何一项规则不同，都可能导致两个市场不能互相对冲，产生真实亏损。勾选表示你已经对比过这一项，而且确认两边一致。</p>
              </section>
              <fieldset className="review-checklist">
                <legend>逐项核对（建议全部看完）</legend>
                {reviewItems.map((item) => (
                  <label key={item.key} className="review-check-item">
                    <input
                      aria-label={`已核对${item.auditLabel}`}
                      type="checkbox"
                      checked={checklist[item.key] === true}
                      onChange={(event) => {
                        const ownsDraft = draftPairId.current === selected.id
                        setChecklist((current) => ({
                          ...(ownsDraft ? current : selected.checklist ?? {}),
                          [item.key]: event.target.checked,
                        }))
                        draftPairId.current = selected.id
                        draftDirty.current = true
                      }}
                    />
                    <span><strong>{item.label}</strong><small>{item.description}</small></span>
                  </label>
                ))}
              </fieldset>
              <div className={uncheckedCount === 0 ? 'review-progress complete' : 'review-progress'}>
                {uncheckedCount === 0
                  ? '10 项都已核对，可以确认配对。'
                  : <><strong>还有 {uncheckedCount} 项未核对</strong><span>请确认剩余项目后再配对；不一致时请选择“不适合配对”。</span></>}
              </div>
              <div className="truth-table">
                <h4>确认后会怎样？</h4>
                <p>系统会把这两个结果视为互补：同一事件中，一个市场赢时，另一个市场应当输。是否真的下单，仍要通过其他风控条件。</p>
              </div>
              <label className="review-notes">
                <span>给自己留个备注（可不填）</span>
                <textarea
                  aria-label="审核备注"
                  value={notes}
                  onChange={(event) => {
                    setNotes(event.target.value)
                    draftPairId.current = selected.id
                    draftDirty.current = true
                  }}
                />
              </label>
              <footer className="review-actions">
                <button className="secondary-button danger-button" type="button" disabled={pending} onClick={() => void submitReview('rejected')}><X size={15} />不适合配对</button>
                <button className="primary-button" type="button" disabled={pending || uncheckedCount > 0} onClick={() => void submitReview('exact')}><Check size={15} />确认可以配对</button>
              </footer>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
