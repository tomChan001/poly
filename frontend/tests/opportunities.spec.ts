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

const viewports = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
]

for (const viewport of viewports) {
  test(`opportunity workspace fits the ${viewport.name} viewport`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await page.route('**/api/opportunities', (route) => route.fulfill({ json: [opportunity] }))
    await page.goto('/')

    await expect(page.getByRole('heading', { name: '跨市场控制台' })).toBeVisible()
    await expect(page.getByText(opportunity.event)).toBeVisible()

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
