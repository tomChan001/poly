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
  id: 'pair-browser',
  title: '2026 年示例事件互补市场',
  kalshi_market_id: 'K-MARKET',
  kalshi_outcome: 'no',
  kalshi_rule_text: '以官方数据源在北京时间截止时刻公布的最终结果为准。',
  kalshi_rule_url: 'https://kalshi.test/rule',
  polymarket_market_id: 'P-TOKEN',
  polymarket_outcome: 'yes',
  polymarket_rule_text: '以官方数据源在北京时间截止时刻公布的最终结果为准。',
  polymarket_rule_url: 'https://poly.test/rule',
  minimum_quantity: '10',
  quantity_step: '1',
  enabled: true,
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
    await page.route('**/api/pairs', (route) => route.fulfill({ json: [pair] }))
    await page.route('**/api/opportunities', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/executions', (route) => route.fulfill({ json: [] }))
    await page.route('**/health', (route) => route.fulfill({
      json: {
        status: 'ok',
        trading_mode: 'limited_auto',
        opening_enabled: true,
        reason: 'configured default',
      },
    }))

    await page.goto('/')
    await expect(page.getByText('自动执行运行中')).toBeVisible()
    await page.getByRole('button', { name: '审核' }).click()
    await expect(page.getByRole('heading', { name: '市场对审核' })).toBeVisible()
    await expect(page.getByRole('heading', { name: pair.title })).toBeVisible()

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
