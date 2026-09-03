import { expect, test } from '@playwright/test'

const opportunity = {
  id: 'opp-visual-check',
  event: '2026 年示例事件是否会在截止日前发生？',
  kalshi_outcome: 'NO',
  polymarket_outcome: 'YES',
  mapping_status: 'exact',
  quantity: '10',
  kalshi_vwap: '0.70',
  polymarket_vwap: '0.20',
  total_fees: '0.02',
  deployed_capital: '9.02',
  payout: '10',
  profit_floor: '0.98',
  conservative_roi: '0.1086',
  expected_settlement_at: '2026-09-01T00:00:00Z',
  worst_case_settlement_at: '2026-09-08T00:00:00Z',
  book_age_ms: 180,
  rejection_reasons: [],
}

const execution = {
  correlation_id: 'corr-history',
  state: 'paired',
  requested_quantity: '10',
  matched_quantity: '10',
  unhedged_quantity: '0',
  legs: {
    kalshi: { client_order_id: 'corr-history-kalshi', status: 'filled', filled_quantity: '10' },
    polymarket: { client_order_id: 'corr-history-polymarket', status: 'filled', filled_quantity: '10' },
  },
  transitions: [
    { source: 'submitted', target: 'paired', occurred_at: '2026-08-18T16:30:00Z' },
  ],
}

const runtime = {
  ready: true,
  running: true,
  opening_enabled: true,
  missing_providers: [],
  last_cycle_at: '2026-08-19T02:00:00Z',
  last_error: null,
  executions_started: 1,
}

async function routeStatus(
  page: import('@playwright/test').Page,
  openingEnabled = true,
) {
  await page.route('**/api/runtime', (route) => route.fulfill({ json: runtime }))
  await page.route('**/health', (route) => route.fulfill({
      json: {
        status: 'ok',
        opening_enabled: openingEnabled,
        reason: 'configured default',
      },
  }))
}

const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
]

for (const viewport of viewports) {
  test(`integration settings fit the ${viewport.name} viewport`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await routeStatus(page, false)
    await page.route('**/api/system-control/opening', async (route) => {
      expect(route.request().method()).toBe('PUT')
      expect(route.request().postDataJSON()).toEqual({
        enabled: true,
        reason: 'operator enabled real ordering',
      })
      await route.fulfill({
        json: {
          opening_enabled: true,
          reason: 'operator enabled real ordering',
          version: 2,
        },
      })
    })
    await page.route('**/api/opportunities', (route) => route.fulfill({ json: [opportunity] }))
    await page.route('**/api/executions', (route) => route.fulfill({ json: [] }))
    await page.route('**/api/integrations', (route) => route.fulfill({ json: [] }))
    await page.goto('/')
    await page.getByRole('button', { name: '集成' }).click()

    await expect(page.getByText('行情评估运行中')).toBeVisible()
    await expect(page.getByText('真实下单已关闭')).toBeVisible()
    await expect(page.getByRole('heading', { name: '集成配置' })).toBeVisible()
    await expect(page.getByText('Google / Magic 登录不需要密码，也不会在这里收集密码。')).toBeVisible()
    await expect(page.getByLabel('Polymarket 账户类型')).toHaveValue('magic_proxy')
    await expect(page.getByLabel('钱包私钥（仅写入）')).toHaveAttribute('type', 'password')
    await expect(page.getByRole('button', { name: '测试连接（不会下单）' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Polymarket' })).toBeVisible()
    await page.getByRole('switch', { name: '真实下单' }).click()
    await expect(page.getByRole('switch', { name: '真实下单' })).toBeChecked()
    await expect(page.getByText('真实下单已开启')).toBeVisible()

    const geometry = await page.evaluate(() => {
      const controls = Array.from(document.querySelectorAll<HTMLElement>('button, input, select'))
        .filter((element) => element.getBoundingClientRect().width > 0)
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
      path: testInfo.outputPath(`integrations-${viewport.name}.png`),
      fullPage: true,
    })
  })
}

for (const viewport of viewports) {
  test(`daily history fits the ${viewport.name} viewport`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await routeStatus(page)
    await page.route('**/api/opportunities', (route) => route.fulfill({ json: [opportunity] }))
    await page.route('**/api/executions', (route) => route.fulfill({ json: [execution] }))
    await page.goto('/')
    await page.getByRole('button', { name: '历史' }).click()

    await expect(page.getByRole('heading', { name: '每日执行历史' })).toBeVisible()
    await expect(page.getByLabel('选择日期')).toHaveValue('2026-08-19')
    await expect(page.getByText('corr-history')).toBeVisible()

    const geometry = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    }))
    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth)

    await page.screenshot({
      path: testInfo.outputPath(`history-${viewport.name}.png`),
      fullPage: true,
    })
  })
}

for (const viewport of viewports) {
  test(`opportunity workspace fits the ${viewport.name} viewport`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await routeStatus(page)
    await page.route('**/api/opportunities', (route) => route.fulfill({ json: [opportunity] }))
    await page.route('**/api/executions', (route) => route.fulfill({ json: [] }))
    await page.goto('/')

    await expect(page.getByRole('heading', { name: '跨市场控制台' })).toBeVisible()
    await expect(page.getByText(opportunity.event)).toBeVisible()
    await expect(page.getByText('行情评估运行中')).toBeVisible()
    await expect(page.getByText('真实下单已开启')).toBeVisible()

    const geometry = await page.evaluate(() => {
      const visibleControls = Array.from(
        document.querySelectorAll<HTMLElement>('button, input'),
      ).filter((element) => {
        const box = element.getBoundingClientRect()
        const style = window.getComputedStyle(element)
        return box.width > 0
          && box.height > 0
          && style.visibility !== 'hidden'
          && style.display !== 'none'
      })
      const overlaps: string[] = []

      for (let leftIndex = 0; leftIndex < visibleControls.length; leftIndex += 1) {
        const left = visibleControls[leftIndex]
        const leftBox = left.getBoundingClientRect()
        for (let rightIndex = leftIndex + 1; rightIndex < visibleControls.length; rightIndex += 1) {
          const right = visibleControls[rightIndex]
          if (left.parentElement !== right.parentElement) continue
          const rightBox = right.getBoundingClientRect()
          const intersects = leftBox.left < rightBox.right
            && leftBox.right > rightBox.left
            && leftBox.top < rightBox.bottom
            && leftBox.bottom > rightBox.top
          if (intersects) {
            overlaps.push(`${left.outerHTML.slice(0, 80)} <> ${right.outerHTML.slice(0, 80)}`)
          }
        }
      }

      return {
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        overlaps,
      }
    })

    expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth)
    expect(geometry.overlaps).toEqual([])
    await page.screenshot({
      path: testInfo.outputPath(`${viewport.name}.png`),
      fullPage: true,
    })
  })
}
