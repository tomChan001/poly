import { useCallback, useEffect, useMemo, useState } from 'react'
import { Check, RefreshCw, X } from 'lucide-react'

import { getPairs, reviewPair } from '../api/client'
import type { ExecutablePair } from '../types/runtime'

const reviewItems = [
  ['subject', '标的主体'],
  ['threshold_boundary', '阈值边界'],
  ['timezone', '时区'],
  ['occurrence_definition', '发生定义'],
  ['data_source', '数据来源'],
  ['postponement', '延期规则'],
  ['cancellation', '取消规则'],
  ['invalid_result', '无效结果'],
  ['dispute_process', '争议流程'],
  ['payout_unit', '结算单位'],
] as const

export function PairSettingsPage() {
  const [pairs, setPairs] = useState<ExecutablePair[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [checklist, setChecklist] = useState<Record<string, boolean>>({})
  const [notes, setNotes] = useState('')
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const selected = useMemo(
    () => pairs.find((pair) => pair.id === selectedId) ?? null,
    [pairs, selectedId],
  )
  const pendingCount = pairs.filter((pair) => pair.status === 'pending_review').length

  const loadPairs = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true)
    try {
      const values = await getPairs()
      setPairs(values)
      setSelectedId((current) => (
        current && values.some((pair) => pair.id === current) ? current : values[0]?.id ?? null
      ))
      setError(null)
    } catch (value) {
      setError(value instanceof Error ? value.message : '读取自动候选失败')
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadPairs(true)
    const timer = window.setInterval(() => void loadPairs(), 5_000)
    return () => window.clearInterval(timer)
  }, [loadPairs])

  useEffect(() => {
    if (!selected) return
    setChecklist(selected.checklist ?? {})
    setNotes(selected.notes)
  }, [selected])

  const submitReview = async (status: 'exact' | 'rejected') => {
    if (!selected) return
    setPending(true)
    setError(null)
    try {
      const saved = await reviewPair(selected.id, {
        status,
        checklist: Object.fromEntries(reviewItems.map(([key]) => [key, checklist[key] === true])),
        truth_table: status === 'exact'
          ? [
              { kalshi: '1', polymarket: '0' },
              { kalshi: '0', polymarket: '1' },
            ]
          : [],
        notes,
      })
      setPairs((current) => current.map((pair) => pair.id === saved.id ? saved : pair))
      setMessage(status === 'exact' ? '审核已保存，可进入自动执行' : '候选已拒绝')
    } catch (value) {
      setError(value instanceof Error ? value.message : '审核保存失败')
    } finally {
      setPending(false)
    }
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

      <div className="pair-workspace">
        <section className="pair-list" aria-label="自动候选列表">
          <header><h3>Oddpool 候选</h3><span>{pairs.length}</span></header>
          {!loading && pairs.length === 0 && (
            <div className="pair-empty">暂无自动发现的待审核候选</div>
          )}
          {pairs.map((pair) => (
            <button
              key={pair.id}
              type="button"
              className={pair.id === selectedId ? 'pair-row selected' : 'pair-row'}
              onClick={() => { setSelectedId(pair.id); setMessage(null) }}
            >
              <strong>{pair.title}</strong>
              <span>{pair.kalshi_market_id} / {pair.polymarket_market_id}</span>
              <b className={`pair-status ${pair.status}`}>{pair.status.toUpperCase()}</b>
            </button>
          ))}
        </section>

        <section className="review-panel">
          {!selected && <div className="pair-empty">等待 Oddpool 自动发现候选</div>}
          {selected && (
            <>
              <header className="review-head">
                <div>
                  <h3>{selected.title}</h3>
                  <span>
                    {selected.kalshi_outcome.toUpperCase()} / {selected.polymarket_outcome.toUpperCase()}
                    {selected.source_candidate_id ? ` · ${selected.source_candidate_id}` : ''}
                  </span>
                </div>
                <b className={`pair-status ${selected.status}`}>{selected.status.toUpperCase()}</b>
              </header>
              <div className="rule-comparison">
                <article><strong>Kalshi</strong><a href={selected.kalshi_rule_url} target="_blank" rel="noreferrer">{selected.kalshi_market_id}</a><p>{selected.kalshi_rule_text}</p></article>
                <article><strong>Polymarket</strong><a href={selected.polymarket_rule_url} target="_blank" rel="noreferrer">{selected.polymarket_market_id}</a><p>{selected.polymarket_rule_text}</p></article>
              </div>
              <fieldset className="review-checklist">
                <legend>规则核对</legend>
                {reviewItems.map(([key, label]) => (
                  <label key={key}>
                    <input
                      aria-label={`已核对${label}`}
                      type="checkbox"
                      checked={checklist[key] === true}
                      onChange={(event) => setChecklist((current) => ({
                        ...current,
                        [key]: event.target.checked,
                      }))}
                    />
                    <span>{label}</span>
                  </label>
                ))}
              </fieldset>
              <div className="truth-table">
                <h4>互补真值表</h4>
                <div><span>情形</span><span>Kalshi</span><span>Polymarket</span></div>
                <div><b>A</b><code>1</code><code>0</code></div>
                <div><b>B</b><code>0</code><code>1</code></div>
              </div>
              <label className="review-notes"><span>审核备注</span><textarea value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
              <footer className="review-actions">
                <button className="secondary-button danger-button" type="button" disabled={pending} onClick={() => void submitReview('rejected')}><X size={15} />拒绝</button>
                <button className="primary-button" type="button" disabled={pending || reviewItems.some(([key]) => checklist[key] !== true)} onClick={() => void submitReview('exact')}><Check size={15} />确认 EXACT</button>
              </footer>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
