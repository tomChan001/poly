import { expect, test } from '@playwright/test'

const runtime = {
  ready: true,
  running: true,
  opening_enabled: true,
  missing_providers: [],
  last_cycle_at: '2026-08-19T02:00:00Z',
  last_error: null,
  executions_started: 3,
}

const pair = {
  preview: {
    eligible: true, rejection_reasons: [], evaluated_at: '2026-08-21T02:01:00Z', risk_policy_version: 'risk-v1',
    conservative_roi: '0.04', gross_roi: '0.08', quantity: '12', kalshi_best_ask: '0.42', polymarket_best_ask: '0.48',
    kalshi_best_ask_quantity: '24', polymarket_best_ask_quantity: '18', paired_liquidity: '18',
    kalshi_vwap: '0.43', polymarket_vwap: '0.49', total_fees: '0.21', deployed_capital: '11.25', profit_floor: '0.45',
  },
  id: 'pair-browser',
  title: '2026 年示例事件互补市场',
  kalshi_market_id: 'K-MARKET',
  kalshi_outcome: 'no',
  kalshi_rule_text: '以官方数据源在北京时间截止时刻公布的最终结果为准。\n\n延期规则：以最终公布结果为准。\n  所有补充规则保留原始换行与缩进。',
  kalshi_rule_url: 'https://kalshi.com/rule',
  kalshi_market_url: 'https://kalshi.com/markets/example/event',
  polymarket_market_id: 'P-TOKEN',
  polymarket_outcome: 'yes',
  polymarket_rule_text: '以官方数据源在北京时间截止时刻公布的最终结果为准。',
  polymarket_rule_url: 'https://polymarket.com/rule',
  polymarket_market_url: 'https://polymarket.com/event/example',
  polymarket_resolution_source: '官方公布结果',
  minimum_quantity: '10',
  quantity_step: '1',
  enabled: true,
  kalshi_expected_settlement_at: '2026-08-25T15:00:00Z',
  polymarket_expected_settlement_at: '2026-08-26T15:00:00Z',
  worst_case_settlement_at: '2026-08-26T15:00:00Z',
  kalshi_category: 'politics',
  polymarket_category: 'news',
  kalshi_minimum_tick: '0.01',
  polymarket_minimum_tick: '0.001',
  native_fingerprint: 'sha256:native-browser',
  material_fingerprint: 'sha256:material-browser',
  status: 'pending_review',
  checklist: null,
  truth_table: [],
  notes: '',
  reviewed_by: null,
  source_candidate_id: 'oddpool-browser',
  source_updated_at: '2026-08-21T02:00:00Z',
}

const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
]

for (const viewport of viewports) {
  test(`runtime and pair review fit the ${viewport.name} viewport`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await page.route('**/api/runtime', (route) => route.fulfill({ json: runtime }))
    await page.route('**/api/pairs?with_preview=true', (route) => route.fulfill({ json: [pair] }))
    await page.route('**/api/opportunities', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/executions', (route) => route.fulfill({ json: [] }))
    await page.route('**/health', (route) => route.fulfill({
      json: {
        status: 'ok',
        opening_enabled: true,
        reason: 'configured default',
      },
    }))

    await page.goto('/')
    await expect(page.getByText('行情评估运行中')).toBeVisible()
    await expect(page.getByText('真实下单已开启')).toBeVisible()
    await page.getByRole('button', { name: '审核' }).click()
    await expect(page.getByRole('heading', { name: '市场对审核' })).toBeVisible()
    await expect(page.getByRole('heading', { name: pair.title })).toBeVisible()
    await expect(page.getByText('关键时间')).toBeVisible()
    await expect(page.getByRole('region', { name: '当前交易预览' }).getByText('4%', { exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: '打开 Kalshi 市场' })).toHaveAttribute('href', pair.kalshi_market_url)
    await expect(page.getByRole('link', { name: '打开 Polymarket 市场' })).toHaveAttribute('href', pair.polymarket_market_url)
    await expect(page.getByText('2026/08/26 23:00').first()).toBeVisible()

    const geometry = await page.evaluate(() => {
      const controls = Array.from(document.querySelectorAll<HTMLElement>('button, input, select, textarea'))
        .filter((element) => {
          const box = element.getBoundingClientRect()
          return box.width > 0 && box.height > 0
        })
        .map((element) => {
          const box = element.getBoundingClientRect()
          return { left: box.left, right: box.right, width: box.width }
        })
      return {
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        controls,
      }
    })

    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth)
    expect(geometry.controls.every((box) => (
      box.width > 0 && box.left >= 0 && box.right <= geometry.viewportWidth
    ))).toBe(true)
    await page.screenshot({
      path: testInfo.outputPath(`runtime-pairs-${viewport.name}.png`),
      fullPage: true,
    })
  })
}
