import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { PairSettingsPage } from './PairSettingsPage'

const desktop = vi.hoisted(() => ({ isTauri: vi.fn(() => false), invoke: vi.fn() }))
vi.mock('@tauri-apps/api/core', () => desktop)

const preview = {
  eligible: true, rejection_reasons: [], evaluated_at: '2026-09-14T01:02:03Z', risk_policy_version: 'risk-preview-v1',
  conservative_roi: '0.04', gross_roi: '0.08', quantity: '12', kalshi_best_ask: '0.42', polymarket_best_ask: '0.48',
  kalshi_best_ask_quantity: '24', polymarket_best_ask_quantity: '18', paired_liquidity: '18',
  kalshi_vwap: '0.43', polymarket_vwap: '0.49', total_fees: '0.21', deployed_capital: '11.25', profit_floor: '0.45',
}
const pair = {
  id: 'qualified', title: '符合条件的候选', status: 'pending_review', kalshi_market_id: 'K', polymarket_market_id: 'P',
  kalshi_outcome: 'no', polymarket_outcome: 'yes', kalshi_rule_url: 'https://kalshi.com/rules/k', polymarket_rule_url: 'https://polymarket.com/rules/p',
  kalshi_market_url: 'https://kalshi.com/markets/k', polymarket_market_url: 'https://polymarket.com/event/p',
  kalshi_rule_text: '完整规则第一行\n\n  保留缩进和后续全部内容', polymarket_rule_text: '另一边完整规则',
  minimum_quantity: '1', quantity_step: '1', enabled: true, notes: '', checklist: null, preview,
}
function respond(pairs: unknown[]) {
  const fetch = vi.fn(() => Promise.resolve({ ok: true, json: async () => pairs }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); desktop.isTauri.mockReturnValue(false); desktop.invoke.mockReset() })

test('defaults to qualified previews and lets reviewers inspect failed and missing evidence', async () => {
  const fetch = respond([pair, { ...pair, id: 'failed', title: '流动性不足候选', preview: { ...preview, eligible: false, rejection_reasons: ['INSUFFICIENT_LIQUIDITY'] } }, { ...pair, id: 'missing', title: '缺少预览候选', preview: undefined }])
  render(<PairSettingsPage />)
  await screen.findByRole('heading', { name: pair.title })
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/pairs?with_preview=true'), expect.objectContaining({ signal: expect.any(AbortSignal) }))
  expect(screen.queryByRole('button', { name: /流动性不足候选/ })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /缺少预览候选/ })).not.toBeInTheDocument()
  expect(screen.getByText('符合条件 1 · 未通过或缺少证据 2 · 全部 3')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('候选筛选'), { target: { value: 'unqualified' } })
  fireEvent.click(screen.getByRole('button', { name: /流动性不足候选/ }))
  expect(screen.getByText('当前最优卖价的配对流动性不足')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /缺少预览候选/ }))
  expect(screen.getByText('缺少评估证据，暂不能判定符合条件。')).toBeInTheDocument()
})

test('shows explicit net and gross ROI, both trade legs, liquidity, fees and market links', async () => {
  respond([pair])
  render(<PairSettingsPage />)
  await screen.findByRole('heading', { name: pair.title })
  const evidence = screen.getByRole('region', { name: '当前交易预览' })
  expect(within(evidence).getByText('4%')).toBeInTheDocument()
  expect(within(evidence).getByText('8%')).toBeInTheDocument()
  for (const text of ['18 份', '$0.42', '$0.48', '24 份', '12 份', '$0.21', '$11.25', '$0.45', 'risk-preview-v1']) {
    expect(within(evidence).getAllByText(text).length).toBeGreaterThan(0)
  }
  expect(screen.getByRole('link', { name: '打开 Kalshi 市场' })).toHaveAttribute('href', pair.kalshi_market_url)
  expect(screen.getByRole('link', { name: '打开 Polymarket 市场' })).toHaveAttribute('href', pair.polymarket_market_url)
  expect(screen.getByText(/完整规则第一行/).textContent).toBe(pair.kalshi_rule_text)
})

test('shows unavailable net ROI when fees are unknown and blocks unsafe platform URLs', async () => {
  respond([{ ...pair, kalshi_market_url: 'javascript:alert(1)', kalshi_rule_url: 'https://kalshi.com.evil.test/rule', polymarket_market_url: 'https://evil.test/event', preview: { ...preview, eligible: false, conservative_roi: null, total_fees: null, rejection_reasons: ['FEE_UNKNOWN'] } }])
  render(<PairSettingsPage />)
  await screen.findByText('当前没有符合条件的候选；可切换筛选查看未通过原因。')
  fireEvent.change(screen.getByLabelText('候选筛选'), { target: { value: 'all' } })
  expect(screen.getByText('费用未确认，净 ROI 暂不可用。')).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '打开 Kalshi 市场' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '打开 Polymarket 市场' })).toHaveAttribute('href', pair.polymarket_rule_url)
  expect(screen.getAllByRole('link').every(link => !link.getAttribute('href')?.includes('evil'))).toBe(true)
})

test('ignores a delayed older refresh after the latest results arrive', async () => {
  const resolvers: Array<(value: unknown) => void> = []
  const signals: AbortSignal[] = []
  vi.stubGlobal('fetch', vi.fn((_input: unknown, init?: RequestInit) => {
    if (init?.signal) signals.push(init.signal)
    return new Promise(resolve => resolvers.push(resolve))
  }))
  render(<PairSettingsPage />)
  fireEvent.click(screen.getByRole('button', { name: '刷新自动候选' }))
  expect(signals[0]?.aborted).toBe(true)
  await act(async () => { resolvers[1]({ ok: true, json: async () => [pair] }) })
  await act(async () => { resolvers[0]({ ok: true, json: async () => [{ ...pair, title: '过时的候选' }] }) })
  expect(screen.getByRole('heading', { name: pair.title })).toBeInTheDocument()
  expect(screen.queryByText('过时的候选')).not.toBeInTheDocument()
})

test('allows a twelve-second preview response to finish without overlapping automatic polls', async () => {
  vi.useFakeTimers()
  try {
    const resolvers: Array<(value: unknown) => void> = []
    const fetch = vi.fn(() => new Promise(resolve => resolvers.push(resolve)))
    vi.stubGlobal('fetch', fetch)
    render(<PairSettingsPage />)
    await act(async () => { vi.advanceTimersByTime(12_000) })
    expect(fetch).toHaveBeenCalledTimes(1)
    await act(async () => { resolvers[0]({ ok: true, json: async () => [pair] }) })
    expect(screen.getByRole('heading', { name: pair.title })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: '已核对标的主体' }))
    fireEvent.change(screen.getByRole('textbox', { name: '审核备注' }), { target: { value: '慢速刷新期间的草稿' } })
    await act(async () => { vi.advanceTimersByTime(3_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    await act(async () => { vi.advanceTimersByTime(12_000) })
    expect(fetch).toHaveBeenCalledTimes(2)
    await act(async () => { resolvers[1]({ ok: true, json: async () => [{ ...pair, title: '最新预览' }] }) })
    expect(screen.getByRole('heading', { name: '最新预览' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '已核对标的主体' })).toBeChecked()
    expect(screen.getByRole('textbox', { name: '审核备注' })).toHaveValue('慢速刷新期间的草稿')
  } finally { vi.useRealTimers() }
})

test('keeps a legacy Kalshi rule URL separate when the event URL is missing', async () => {
  respond([{ ...pair, kalshi_market_url: undefined, kalshi_rule_url: 'https://kalshi.com/markets/contract-ticker' }])
  render(<PairSettingsPage />)
  await screen.findByRole('heading', { name: pair.title })
  expect(screen.queryByRole('link', { name: '打开 Kalshi 市场' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '查看 Kalshi 规则来源' })).toHaveAttribute('href', 'https://kalshi.com/markets/contract-ticker')
})

test.each([true, false])('clears a dirty checklist when material rules change (fingerprint supplied: %s)', async (supplied) => {
  let response = { ...pair, checklist: { subject: false }, material_fingerprint: supplied ? 'rules-v1' : undefined }
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: async () => [response] })))
  render(<PairSettingsPage />)
  await screen.findByRole('heading', { name: pair.title })
  fireEvent.click(screen.getByRole('checkbox', { name: '已核对标的主体' }))
  fireEvent.change(screen.getByRole('textbox', { name: '审核备注' }), { target: { value: '旧规则的未保存备注' } })
  response = { ...response, checklist: { subject: true }, kalshi_rule_text: '规则变更后以新公布结果为准', material_fingerprint: supplied ? 'rules-v2' : undefined }
  fireEvent.click(screen.getByRole('button', { name: '刷新自动候选' }))
  await screen.findByText('规则变更后以新公布结果为准')
  await waitFor(() => expect(screen.getByRole('checkbox', { name: '已核对标的主体' })).not.toBeChecked())
  expect(screen.getByRole('textbox', { name: '审核备注' })).toHaveValue('')
})

test('expires supplied preview validity even while the next refresh is pending', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-14T01:02:03Z'))
  try {
    let requests = 0
    vi.stubGlobal('fetch', vi.fn(() => {
      requests += 1
      return requests === 1
        ? Promise.resolve({ ok: true, json: async () => [{ ...pair, preview: { ...preview, valid_until: '2026-09-14T01:02:10Z' } }] })
        : new Promise(() => {})
    }))
    render(<PairSettingsPage />)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('heading', { name: pair.title })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: '已核对标的主体' }))
    await act(async () => { vi.advanceTimersByTime(4_000) })
    expect(requests).toBe(2)
    await act(async () => { vi.advanceTimersByTime(4_000) })
    expect(requests).toBe(2)
    expect(screen.queryByRole('button', { name: /符合条件的候选/ })).not.toBeInTheDocument()
    expect(screen.getByText('符合条件 0 · 未通过或缺少证据 1 · 全部 1')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: pair.title })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: '已核对标的主体' })).toBeChecked()
    fireEvent.change(screen.getByLabelText('候选筛选'), { target: { value: 'all' } })
    expect(screen.getByText('行情已过期，请等待重新评估。')).toBeInTheDocument()
  } finally { vi.useRealTimers() }
})

test('opens market and rule links with the fixed native command inside the desktop app', async () => {
  desktop.isTauri.mockReturnValue(true)
  desktop.invoke.mockResolvedValue(undefined)
  respond([pair])
  render(<PairSettingsPage />)
  await screen.findByRole('heading', { name: pair.title })
  fireEvent.click(screen.getByRole('link', { name: '打开 Kalshi 市场' }))
  fireEvent.click(screen.getByRole('link', { name: '打开 Polymarket 市场' }))
  fireEvent.click(screen.getByRole('link', { name: '查看 Kalshi 规则来源' }))
  expect(desktop.invoke).toHaveBeenNthCalledWith(1, 'open_market_url', { url: pair.kalshi_market_url })
  expect(desktop.invoke).toHaveBeenNthCalledWith(2, 'open_market_url', { url: pair.polymarket_market_url })
  expect(desktop.invoke).toHaveBeenNthCalledWith(3, 'open_market_url', { url: pair.kalshi_rule_url })
})

test('reports native browser opening failure on the review page', async () => {
  desktop.isTauri.mockReturnValue(true)
  desktop.invoke.mockRejectedValue(new Error('open failed'))
  respond([pair])
  render(<PairSettingsPage />)
  fireEvent.click(await screen.findByRole('link', { name: '打开 Kalshi 市场' }))
  expect(await screen.findByText('无法打开系统浏览器，请稍后重试。')).toBeInTheDocument()
})
